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

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


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
            "timeout": httpx.Timeout(30.0, connect=10.0, read=30.0),
            "auth": auth,
            "headers": headers,
            "follow_redirects": True,
        }

    async def analytics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Query the DHIS2 Analytics API."""
        return await self._get("/api/analytics.json", params=params)

    async def tracker(self, params: dict[str, Any]) -> dict[str, Any]:
        """Query the DHIS2 Tracker Events API."""
        return await self._get("/api/tracker/events.json", params=params)
        
    async def data_value_sets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Query the DHIS2 Data Value Sets API for raw data."""
        return await self._get("/api/dataValueSets.json", params=params)

    async def geojson(self, level: int, parent_uid: str) -> dict[str, Any]:
        """Fetch GeoJSON boundaries for organisation units."""
        params = {
            "level": level,
            "parent": parent_uid
        }
        return await self._get("/api/organisationUnits.geojson", params=params)

    async def metadata(
        self,
        endpoint: str,
        fields: str = "id,name,shortName,description",
        page_size: int = 1000,
        max_pages: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch ALL metadata objects from a DHIS2 metadata endpoint, paginated.

        Real instances hold tens of thousands of org units / data elements; a
        single unpaged pull routinely exceeds the read timeout. This follows the
        response ``pager`` (pageSize per request) until exhausted.

        Args:
            endpoint: e.g. "dataElements", "indicators", "organisationUnits"
            fields: DHIS2 field filter
            page_size: objects per page request
            max_pages: hard cap to guard against a pager that never terminates
        """
        results: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            params: dict[str, Any] = {
                "fields": fields,
                "paging": "true",
                "pageSize": page_size,
                "page": page,
            }
            data = await self._get(f"/api/{endpoint}.json", params=params)

            # DHIS2 wraps results in a key matching the endpoint name, alongside
            # a "pager" dict. Take the first list-valued key.
            items = next((v for v in data.values() if isinstance(v, list)), [])
            results.extend(items)

            pager = data.get("pager") or {}
            # Stop when the server signals no further pages (no nextPage), when a
            # page came back empty, or when the response wasn't paged at all.
            if not items or not pager.get("nextPage"):
                break
            page += 1

        if page > max_pages:
            logger.warning(
                "metadata_pagination_cap_reached",
                extra={"endpoint": endpoint, "max_pages": max_pages, "fetched": len(results)},
            )
        return results

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Generic GET method for DHIS2 API endpoints."""
        return await self._get(path, params=params)

    async def me(self) -> dict[str, Any]:
        """Fetch current user profile — used for token validation."""
        return await self._get("/api/me.json")

    async def health_check(self) -> dict[str, Any]:
        """Validate DHIS2 connectivity via /api/system/info.json.

        Returns system info dict on success. Raises on any failure.
        """
        return await self._get("/api/system/info.json")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with bounded retries on transient transport and server errors."""
        kwargs = self._build_client_kwargs()
        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    response = await client.get(path, params=params)
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_ATTEMPTS - 1:
                        logger.warning(
                            "dhis2_request_retryable_status",
                            extra={
                                "path": path,
                                "status": response.status_code,
                                "attempt": attempt + 1,
                                "duration_ms": int((time.monotonic() - start) * 1000),
                            },
                        )
                        await _sleep_before_retry(attempt)
                        continue
                    response.raise_for_status()
                    # A 200 text/html body means we followed a redirect to the
                    # DHIS2 login page — i.e. the request was unauthenticated.
                    # Surface that clearly instead of a cryptic JSON decode error.
                    content_type = response.headers.get("content-type", "")
                    if "text/html" in content_type.lower():
                        raise RuntimeError(
                            "DHIS2 returned an HTML page instead of JSON — the request was "
                            "likely redirected to the login page (missing/invalid credentials)."
                        )
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("DHIS2 returned a non-object JSON payload.")
                    logger.info(
                        "dhis2_request_ok",
                        extra={
                            "path": path,
                            "status": response.status_code,
                            "attempt": attempt + 1,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                            "params_shape": _params_shape(params),
                            "row_count": len(payload.get("rows", [])) if isinstance(payload, dict) else None,
                        },
                    )
                    return payload
            except httpx.TransportError as exc:
                # Base class of ConnectError/ReadTimeout/ConnectTimeout AND the
                # transient failures previously missed (RemoteProtocolError,
                # ReadError, WriteError, PoolTimeout) — all worth retrying.
                last_exc = exc
                if attempt < MAX_ATTEMPTS - 1:
                    logger.warning(
                        "dhis2_transport_retry",
                        extra={"path": path, "attempt": attempt + 1, "error_type": type(exc).__name__},
                    )
                    await _sleep_before_retry(attempt)
                    continue
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:500]
                is_html = "<html" in body.lower() or "<!doctype" in body.lower()

                # Extract structured error message from DHIS2 JSON error responses
                dhis2_error_msg = ""
                if not is_html and exc.response.status_code in {409, 400}:
                    try:
                        err_json = exc.response.json()
                        dhis2_error_msg = err_json.get("message", "")
                        if not dhis2_error_msg:
                            # Some DHIS2 versions nest the message differently
                            dhis2_error_msg = err_json.get("status", "")
                    except Exception:
                        pass

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
                        f"dhis2_http_error: HTTP {exc.response.status_code} - "
                        f"{dhis2_error_msg or body}",
                        extra={
                            "path": path,
                            "full_url": f"{self._base_url}{path}",
                            "params_shape": _params_shape(params),
                            "status": exc.response.status_code,
                            "dhis2_message": dhis2_error_msg,
                            "duration_ms": int((time.monotonic() - start) * 1000),
                        },
                    )
                raise
            except Exception as exc:
                logger.error(
                    "dhis2_request_error",
                    extra={"path": path, "params_shape": _params_shape(params), "error": str(exc)},
                    exc_info=True
                )
                raise

        raise last_exc or RuntimeError(f"DHIS2 request failed after {MAX_ATTEMPTS} attempts: {path}")


async def _sleep_before_retry(attempt: int) -> None:
    import asyncio

    await asyncio.sleep(0.25 * (attempt + 1))


def _params_shape(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    shaped: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, list):
            shaped[key] = {"count": len(value), "values": value}
        else:
            shaped[key] = value
    return shaped
