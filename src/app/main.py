from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import chat_completions, health, models, sessions, web
from app.config import Settings, get_settings
from app.runtime import AgentSDKRuntime, ClaudeCodeRuntime, FakeRuntime
from app.runtime.model_catalog import ModelCatalog
from app.sandbox import DockerSandboxManager, LocalSandboxManager
from app.sessions import SessionLockRegistry, SQLiteSessionRepository
from app.sessions.html_log import SessionHtmlLogger
from app.sessions.reaper import LifecycleReaper
from app.sessions.service import (
    InvalidWorkspacePathError,
    SessionBackendMismatchError,
    SessionBusyError,
    SessionDeletedError,
    SessionNotFoundError,
    SessionService,
    SessionServiceError,
)
from app.sessions.ui_events import ActiveTurnRegistry, UiEventJournal


def _database_path(database_url: str) -> str:
    return (
        database_url.removeprefix("sqlite:///")
        if database_url.startswith("sqlite:///")
        else database_url
    )


def _sandbox(settings: Settings):
    if settings.sandbox_backend == "local":
        return LocalSandboxManager(Path(settings.workspace_root))
    if settings.sandbox_backend == "docker":
        return DockerSandboxManager(
            Path(settings.workspace_root),
            image=settings.docker_image,
            docker_binary=settings.docker_binary,
            network_mode=settings.docker_network_mode,
            cpus=str(settings.docker_cpus),
            memory=settings.docker_memory,
            pids_limit=settings.docker_pids_limit,
        )
    raise RuntimeError(f"Unsupported sandbox backend: {settings.sandbox_backend}")


def _docker_claude_executor(sandbox: DockerSandboxManager, timeout_seconds: int):
    async def execute(
        command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> AsyncIterator[str]:
        # Claude receives a host workspace path from the durable session mapping,
        # while the same bind mount lives at /workspace inside the worker.
        container_env = dict(env)
        config_dir = Path(container_env["CLAUDE_CONFIG_DIR"])
        try:
            relative_config = config_dir.relative_to(cwd)
        except ValueError as exc:
            raise RuntimeError("Claude config must stay inside its session workspace") from exc
        container_env["CLAUDE_CONFIG_DIR"] = str(Path("/workspace") / relative_config)
        # Docker gets environment values through argv, not a shell.  Remove the
        # host-only virtualenv marker so commands resolve against the worker image.
        container_env.pop("VIRTUAL_ENV", None)
        async for line in sandbox.stream_exec(
            cwd.name,
            list(command),
            env=container_env,
            timeout_seconds=timeout_seconds,
        ):
            yield line

    return execute


def _runtime(settings: Settings, sandbox):
    if settings.runtime_backend == "fake":
        return FakeRuntime(settings.fake_stream_delay_ms, settings.fake_long_task_delay_ms)
    if settings.runtime_backend == "zhipu":
        if not settings.zhipu_api_key:
            raise RuntimeError("ZHIPU_API_KEY is required when RUNTIME_BACKEND=zhipu")
        from app.runtime.zhipu import ZhipuRuntime

        return ZhipuRuntime(
            api_key=settings.zhipu_api_key,
            model=settings.zhipu_model,
            endpoint=settings.zhipu_base_url.rstrip("/") + "/chat/completions",
        )
    if settings.runtime_backend == "claude":
        if not isinstance(sandbox, DockerSandboxManager):
            raise RuntimeError("RUNTIME_BACKEND=claude requires SANDBOX_BACKEND=docker")
        executor = _docker_claude_executor(sandbox, settings.claude_timeout_seconds)
        return AgentSDKRuntime(
            api_key=settings.claude_api_key,
            base_url=settings.claude_base_url,
            model=settings.claude_model,
            auth_env=settings.claude_auth_env,
            runner_command=settings.claude_sdk_runner,
            executor=executor,
        )
    if settings.runtime_backend == "claude-cli":
        if not settings.claude_api_key:
            raise RuntimeError("CLAUDE_API_KEY is required when RUNTIME_BACKEND=claude-cli")
        if not isinstance(sandbox, DockerSandboxManager):
            raise RuntimeError("RUNTIME_BACKEND=claude-cli requires SANDBOX_BACKEND=docker")
        executor = _docker_claude_executor(sandbox, settings.claude_timeout_seconds)
        return ClaudeCodeRuntime(
            api_key=settings.claude_api_key,
            base_url=settings.claude_base_url,
            model=settings.claude_model,
            auth_env=settings.claude_auth_env,
            claude_command=settings.claude_command,
            executor=executor,
        )
    raise RuntimeError(f"Unsupported runtime backend: {settings.runtime_backend}")


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository = SQLiteSessionRepository(_database_path(configured.database_url))
        await repository.initialize()
        ui_event_journal = UiEventJournal(repository)
        await ui_event_journal.start()
        active_turns = ActiveTurnRegistry(ui_event_journal)
        sandbox = _sandbox(configured)
        runtime = _runtime(configured, sandbox)
        model_catalog = None
        if configured.runtime_backend in {"claude", "claude-cli"}:
            model_catalog = ModelCatalog(
                api_key=configured.claude_api_key or "",
                base_url=configured.claude_base_url,
                auth_env=configured.claude_auth_env,
                fallback_models=configured.model_catalog_fallback_models,
                cache_seconds=configured.model_catalog_cache_seconds,
            )
        session_logger = SessionHtmlLogger(repository)
        service = SessionService(
            repository,
            SessionLockRegistry(),
            sandbox,
            runtime,
            delete_workspace=configured.session_delete_workspace,
            html_logger=session_logger,
            delete_after_seconds=configured.session_delete_after_seconds,
        )
        reaper = LifecycleReaper(
            service,
            pause_after_seconds=configured.session_pause_after_seconds,
            delete_after_seconds=configured.session_delete_after_seconds,
            interval_seconds=configured.session_reaper_interval_seconds,
        )
        task = asyncio.create_task(reaper.run(), name="session-lifecycle-reaper")

        app.state.settings = configured
        app.state.repository = repository
        app.state.ui_event_journal = ui_event_journal
        app.state.active_turns = active_turns
        app.state.sandbox_manager = sandbox
        app.state.runtime = runtime
        app.state.model_catalog = model_catalog
        app.state.session_service = service
        app.state.session_logger = session_logger
        app.state.chat_completion_handler = service

        async def health_check() -> dict[str, str]:
            return {
                "status": "ok",
                "database": "ok",
                "sandbox": configured.sandbox_backend,
                "runtime": configured.runtime_backend,
            }

        app.state.health_check = health_check
        try:
            yield
        finally:
            # Stop lifecycle mutations first. Browser disconnects only detach;
            # application shutdown is the sole owner that aborts background
            # turns, joins their SessionService locks, then drains UI history.
            reaper.stop()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await active_turns.close_all()
            await ui_event_journal.close()
            await repository.close()
            close = getattr(runtime, "aclose", None)
            if close is not None:
                await close()
            if model_catalog is not None:
                await model_catalog.aclose()

    app = FastAPI(title="WebAgent", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(SessionServiceError)
    async def session_error_handler(_: Request, exc: SessionServiceError) -> JSONResponse:
        if isinstance(exc, SessionDeletedError):
            status_code = 410
        elif isinstance(exc, InvalidWorkspacePathError):
            status_code = 400
        elif isinstance(exc, (SessionBusyError, SessionBackendMismatchError)):
            status_code = 409
        elif isinstance(exc, SessionNotFoundError):
            status_code = 404
        else:
            status_code = 503
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {"message": str(exc), "type": "invalid_request_error", "code": exc.code}
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            content = exc.detail
        else:
            error_type = (
                "authentication_error" if exc.status_code == 401 else "invalid_request_error"
            )
            content = {
                "error": {
                    "message": str(exc.detail),
                    "type": error_type,
                    "code": "http_error",
                }
            }
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        messages = [error.get("msg", "Invalid request") for error in exc.errors()]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "; ".join(messages),
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                }
            },
        )

    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat_completions.router)
    app.include_router(sessions.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.web_root), name="static")
    return app


app = create_app()
