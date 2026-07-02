"""DHIS2 HTTP client — analytics + metadata endpoints.

Supports:
- Personal Access Token (PAT) auth via Authorization header
- Service account basic auth fallback
- Retry on connection error (1 retry)
- Configurable timeout
- Structured error logging
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from backend.config import Settings

logger = logging.getLogger("dhis2_analyst.dhis2_client")


class DHIS2Client:
    def __init__(self, settings: Settings, token: str | None = None) -> None:
        self.settings = settings
        self.token = token
        self._base_url = settings.dhis2_base_url.rstrip("/")

    def _build_client_kwargs(self) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        auth = None

        if self.token:
            headers["Authorization"] = f"ApiToken {self.token}"
        elif self.settings.dhis2_service_account_user and self.settings.dhis2_service_account_pass:
            auth = (
                self.settings.dhis2_service_account_user,
                self.settings.dhis2_service_account_pass,
            )

        return {
            "base_url": self._base_url,
            "timeout": 30.0,
            "auth": auth,
            "headers": headers,
            "follow_redirects": True,
        }

    async def analytics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Query the DHIS2 Analytics API."""
        return await self._get("/api/analytics.json", params=params)

    async def metadata(
        self,
        endpoint: str,
        fields: str = "id,name,shortName,description",
        paging: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch metadata objects from a DHIS2 metadata endpoint.

        Args:
            endpoint: e.g. "dataElements", "indicators", "organisationUnits"
            fields: DHIS2 field filter
            paging: Whether to request paged results
        """
        params: dict[str, Any] = {
            "fields": fields,
            "paging": str(paging).lower(),
        }
        data = await self._get(f"/api/{endpoint}.json", params=params)

        # DHIS2 wraps results in a key matching the endpoint name
        # e.g. {"dataElements": [...]} or {"indicators": [...]}
        for key in data:
            if isinstance(data[key], list):
                return data[key]
        return []

    async def me(self) -> dict[str, Any]:
        """Fetch current user profile — used for token validation."""
        return await self._get("/api/me.json")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with 1 retry on connection error."""
        kwargs = self._build_client_kwargs()
        last_exc: Exception | None = None

        for attempt in range(2):
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    response = await client.get(path, params=params)
                    response.raise_for_status()
                    logger.info(
                        "dhis2_request_ok",
                        extra={
                            "path": path,
                            "status": response.status_code,
                            "attempt": attempt + 1,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                        },
                    )
                    return response.json()
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "dhis2_connect_retry",
                        extra={"path": path, "attempt": attempt + 1},
                    )
                    continue
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:500]
                is_html = "<html" in body.lower() or "<!doctype" in body.lower()
                if is_html and exc.response.status_code == 404:
                    logger.error(
                        "dhis2_base_url_wrong: Got HTML 404 from nginx — "
                        "DHIS2_BASE_URL likely points at a frontend wrapper, not the API host. "
                        f"Current base: {self._base_url}",
                        extra={
                            "path": path,
                            "full_url": f"{self._base_url}{path}",
                            "status": 404,
                            "hint": "Check DHIS2_BASE_URL in .env — try the play.im.dhis2.org host",
                        },
                    )
                else:
                    logger.error(
                        f"dhis2_http_error: HTTP {exc.response.status_code} - {body}",
                        extra={
                            "path": path,
                            "full_url": f"{self._base_url}{path}",
                            "status": exc.response.status_code,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                        },
                    )
                raise
            except Exception as exc:
                logger.error(
                    "dhis2_request_error",
                    extra={"path": path, "error": str(exc)},
                )
                raise

        raise last_exc or RuntimeError(f"DHIS2 request failed after retries: {path}")
