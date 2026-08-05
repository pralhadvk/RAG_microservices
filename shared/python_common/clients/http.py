"""HTTP client with OpenTelemetry context propagation."""

from __future__ import annotations

from typing import Any

import httpx
from opentelemetry.propagate import inject


class TracedHttpClient:
    """Thin async HTTP wrapper that injects W3C trace headers."""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        out_headers: dict[str, str] = dict(headers or {})
        inject(out_headers)
        return await self._client.post(path, json=json, headers=out_headers)

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        out_headers: dict[str, str] = dict(headers or {})
        inject(out_headers)
        return await self._client.get(path, headers=out_headers)
