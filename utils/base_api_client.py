from __future__ import annotations

from typing import Any

import httpx


class BaseAPIClient:
    def __init__(self, base_url: str, timeout: float = 30.0, headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        merged_headers = {**self.headers, **(headers or {})}
        with httpx.Client(base_url=self.base_url, timeout=self.timeout, headers=merged_headers) as client:
            response = client.request(
                method=method.upper(),
                url=path,
                params=params,
                json=json,
                data=data,
                files=files,
            )
            response.raise_for_status()
            return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

