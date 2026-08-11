"""Provider-neutral cached discovery of Anthropic-compatible model IDs."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


class ModelCatalogUnavailableError(RuntimeError):
    """The configured provider could not supply a usable model directory."""

    def __init__(self, reason: str, *, endpoint: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.endpoint = endpoint


class ModelCatalog:
    """Fetch ``/v1/models`` once per short TTL and retain a safe fallback."""

    _allowed_auth_envs = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        auth_env: str,
        fallback_models: Sequence[str],
        cache_seconds: float = 300,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if auth_env not in self._allowed_auth_envs:
            raise ValueError(f"Unsupported auth environment: {auth_env}")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else None
        self.auth_env = auth_env
        self.fallback_models = tuple(dict.fromkeys(model for model in fallback_models if model))
        self.cache_seconds = max(cache_seconds, 0)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None
        self.clock = clock
        self._cached: tuple[str, ...] | None = None
        self._cached_from_provider = False
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def endpoint(self) -> str | None:
        return f"{self.base_url}/v1/models" if self.base_url else None

    @property
    def safe_endpoint(self) -> str:
        """A request target suitable for diagnostics, without URL credentials or queries."""
        endpoint = self.endpoint
        if not endpoint:
            return "<未配置>"
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    @property
    def api_key_fingerprint(self) -> str:
        """Short, non-reversible diagnostic identifier; never sent to clients."""
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:10]

    @property
    def headers(self) -> dict[str, str]:
        if self.auth_env == "ANTHROPIC_AUTH_TOKEN":
            return {"Authorization": f"Bearer {self.api_key}"}
        return {"x-api-key": self.api_key}

    async def models(self) -> tuple[str, ...]:
        if self._cached is not None and self.clock() < self._expires_at:
            return self._cached
        async with self._lock:
            if self._cached is not None and self.clock() < self._expires_at:
                return self._cached
            discovered = await self._fetch()
            self._cached = discovered or self.fallback_models
            self._cached_from_provider = bool(discovered)
            self._expires_at = self.clock() + self.cache_seconds
            return self._cached

    async def discover(self) -> tuple[str, ...]:
        """Strict provider discovery for an explicitly client-configured endpoint.

        Unlike :meth:`models`, this never substitutes deployment fallbacks: the
        browser must receive a clear error when its own provider configuration
        is invalid or unavailable.
        """

        if (
            self._cached is not None
            and self._cached_from_provider
            and self.clock() < self._expires_at
        ):
            return self._cached
        async with self._lock:
            if (
                self._cached is not None
                and self._cached_from_provider
                and self.clock() < self._expires_at
            ):
                return self._cached
            discovered = await self._fetch(strict=True)
            self._cached = discovered
            self._cached_from_provider = True
            self._expires_at = self.clock() + self.cache_seconds
            return discovered

    async def _fetch(self, *, strict: bool = False) -> tuple[str, ...]:
        endpoint = self.endpoint
        if not endpoint or not self.api_key:
            return self._unavailable("未配置 Provider Endpoint 或 API Key", strict=strict)
        try:
            response = await self.client.get(endpoint, headers=self.headers)
        except httpx.TimeoutException:
            return self._unavailable("上游请求超时", strict=strict)
        except httpx.RequestError:
            return self._unavailable("上游网络不可达", strict=strict)
        if not 200 <= response.status_code < 300:
            return self._unavailable(f"上游返回 HTTP 状态 {response.status_code}", strict=strict)
        try:
            payload: Any = response.json()
        except ValueError:
            return self._unavailable("上游返回的 JSON 无效", strict=strict)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return self._unavailable("上游响应缺少 data 模型列表", strict=strict)
        models = tuple(
            dict.fromkeys(
                item["id"].strip()
                for item in payload["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
            )
        )
        if not models:
            return self._unavailable("上游模型目录为空", strict=strict)
        return models

    def _unavailable(self, reason: str, *, strict: bool) -> tuple[str, ...]:
        if strict:
            raise ModelCatalogUnavailableError(reason, endpoint=self.safe_endpoint)
        return ()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
