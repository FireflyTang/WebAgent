"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings with safe, runnable local-demo defaults."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"
    api_key: str = "demo-local-key"
    runtime_backend: str = "fake"
    database_url: str = "sqlite:///./data/demo.db"
    sandbox_backend: str = "docker"
    workspace_root: Path = Path("./data/workspaces")
    docker_image: str = "webagent-worker:latest"
    docker_binary: str = "docker"
    docker_network_mode: str = "none"
    docker_cpus: float = Field(default=1.0, gt=0)
    docker_memory: str = "512m"
    docker_pids_limit: int = Field(default=128, ge=16)
    session_pause_after_seconds: int = Field(default=1800, ge=1)
    session_delete_after_seconds: int = Field(default=7200, ge=1)
    session_reaper_interval_seconds: int = Field(default=30, ge=1)
    session_delete_workspace: bool = True
    fake_stream_delay_ms: int = Field(default=80, ge=0)
    fake_long_task_delay_ms: int = Field(default=500, ge=0)
    zhipu_api_key: str | None = None
    zhipu_model: str = "glm-4.5-air"
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    claude_api_key: str | None = None
    claude_base_url: str | None = None
    claude_model: str | None = None
    claude_available_models: str = ""
    model_catalog_cache_seconds: int = Field(default=300, ge=0)
    claude_auth_env: str = "ANTHROPIC_API_KEY"
    claude_command: str = "claude"
    claude_sdk_runner: str = "/usr/local/bin/oca-agent-sdk-runner"
    claude_timeout_seconds: int = Field(default=600, ge=30)

    @property
    def selectable_models(self) -> tuple[str, ...]:
        configured = tuple(
            dict.fromkeys(
                model.strip() for model in self.claude_available_models.split(",") if model.strip()
            )
        )
        if configured:
            return configured
        if self.runtime_backend in {"claude", "claude-cli"} and self.claude_model:
            return (self.claude_model,)
        if self.runtime_backend == "zhipu":
            return (self.zhipu_model,)
        return ("claude-code-agent",)

    @property
    def model_catalog_fallback_models(self) -> tuple[str, ...]:
        """Provider model names usable if remote model discovery is unavailable."""
        configured = tuple(
            dict.fromkeys(
                model.strip() for model in self.claude_available_models.split(",") if model.strip()
            )
        )
        if self.claude_model:
            return tuple(dict.fromkeys((self.claude_model, *configured)))
        return configured or self.selectable_models

    @property
    def default_web_model(self) -> str:
        preferred = self.claude_model if self.runtime_backend in {"claude", "claude-cli"} else None
        return preferred if preferred in self.selectable_models else self.selectable_models[0]


@lru_cache
def get_settings() -> Settings:
    return Settings()
