from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, health, sessions, users, web
from app.config import Settings, get_settings
from app.monitoring import DockerProbe, SystemMonitor
from app.runtime import AgentSDKRuntime, ClaudeCodeRuntime, FakeRuntime
from app.sandbox import DockerSandboxManager, LocalSandboxManager
from app.sessions import SessionLockRegistry, SQLiteSessionRepository
from app.sessions.html_log import SessionHtmlLogger
from app.sessions.reaper import LifecycleReaper
from app.sessions.service import (
    FileChangedError,
    FileTooLargeError,
    FileUploadLimitError,
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
    if settings.runtime_backend == "claude":
        if not isinstance(sandbox, DockerSandboxManager):
            raise RuntimeError("RUNTIME_BACKEND=claude requires SANDBOX_BACKEND=docker")
        executor = _docker_claude_executor(sandbox, settings.claude_timeout_seconds)
        return AgentSDKRuntime(
            runner_command=settings.claude_sdk_runner,
            executor=executor,
        )
    if settings.runtime_backend == "claude-cli":
        if not isinstance(sandbox, DockerSandboxManager):
            raise RuntimeError("RUNTIME_BACKEND=claude-cli requires SANDBOX_BACKEND=docker")
        executor = _docker_claude_executor(sandbox, settings.claude_timeout_seconds)
        return ClaudeCodeRuntime(
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
        managed_settings = await repository.get_managed_settings()
        effective = Settings(**{**configured.model_dump(), **dict(managed_settings.values)})
        ui_event_journal = UiEventJournal(repository)
        await ui_event_journal.start()
        active_turns = ActiveTurnRegistry(ui_event_journal)
        sandbox = _sandbox(effective)
        runtime = _runtime(effective, sandbox)
        session_logger = SessionHtmlLogger(repository)
        service = SessionService(
            repository,
            SessionLockRegistry(),
            sandbox,
            runtime,
            delete_workspace=effective.session_delete_workspace,
            html_logger=session_logger,
            delete_after_seconds=effective.session_delete_after_seconds,
        )
        reaper = LifecycleReaper(
            service,
            pause_after_seconds=effective.session_pause_after_seconds,
            delete_after_seconds=effective.session_delete_after_seconds,
            interval_seconds=effective.session_reaper_interval_seconds,
        )
        task = asyncio.create_task(reaper.run(), name="session-lifecycle-reaper")
        monitor = SystemMonitor(
            repository,
            ui_event_journal,
            active_turns,
            reaper,
            task,
            Path(effective.workspace_root),
            DockerProbe(
                effective.docker_binary,
                effective.docker_image,
                enabled=isinstance(sandbox, DockerSandboxManager),
            ),
        )
        await monitor.start()

        app.state.settings = effective
        app.state.repository = repository
        app.state.ui_event_journal = ui_event_journal
        app.state.active_turns = active_turns
        app.state.sandbox_manager = sandbox
        app.state.runtime = runtime
        app.state.session_service = service
        app.state.session_logger = session_logger
        app.state.system_monitor = monitor

        async def health_check() -> dict[str, str]:
            return {
                "status": "ok",
                "database": "ok",
                "sandbox": effective.sandbox_backend,
                "runtime": effective.runtime_backend,
            }

        app.state.health_check = health_check
        try:
            yield
        finally:
            # Stop lifecycle mutations first. Browser disconnects only detach;
            # application shutdown is the sole owner that aborts background
            # turns, joins their SessionService locks, then drains UI history.
            await monitor.close()
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

    app = FastAPI(title="WebAgent", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(SessionServiceError)
    async def session_error_handler(_: Request, exc: SessionServiceError) -> JSONResponse:
        if isinstance(exc, SessionDeletedError):
            status_code = 410
        elif isinstance(exc, InvalidWorkspacePathError):
            status_code = 400
        elif isinstance(exc, (FileTooLargeError, FileUploadLimitError)):
            status_code = 413
        elif isinstance(exc, (SessionBusyError, SessionBackendMismatchError, FileChangedError)):
            status_code = 409
        elif isinstance(exc, SessionNotFoundError):
            status_code = 404
        else:
            status_code = 503
        return JSONResponse(
            status_code=status_code,
            content={"error": {"message": str(exc), "code": exc.code}},
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        content = {"error": {"message": str(exc.detail), "code": "http_error"}}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        messages = [error.get("msg", "Invalid request") for error in exc.errors()]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "; ".join(messages),
                    "code": "invalid_request",
                }
            },
        )

    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(admin.router)
    app.include_router(sessions.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.web_root), name="static")
    return app


app = create_app()
