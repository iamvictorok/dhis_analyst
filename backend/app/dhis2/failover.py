"""DHIS2 live -> fallback -> cached-snapshot failover.

Why: the public demo instances (play.im.dhis2.org) go down or get reset without
notice. For a live demo we want, in order:

1. **live**     - the primary instance (first entry of ``DHIS2_BASE_URLS``).
2. **fallback** - the first other configured instance that answers its health
                  check (e.g. the 2.40 / 2.42 stable play instances).
3. **cached**   - when every instance is down and ``DHIS2_SNAPSHOT_MODE=auto``,
                  serve recorded Analytics/metadata responses from the committed
                  demo snapshot, and label every answer
                  "Cached data as of <date>".

``DHIS2_SNAPSHOT_MODE=off`` disables the snapshot entirely; ``always`` forces it
(useful for rehearsals with no network).

The module keeps one process-wide status (the app runs a single worker) and a
per-request tracker so the agent can tell which source actually served the
data for *this* answer.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("dhis2_analyst.failover")

MODES_ORDER = {"live": 0, "fallback": 1, "cached": 2}

DEFAULT_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2] / "demo_snapshot" / "dhis2_snapshot.json"
)


class SnapshotMiss(RuntimeError):
    """The request is not in the demo snapshot (and DHIS2 is unreachable)."""


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def base_urls(settings) -> list[str]:
    raw = getattr(settings, "dhis2_base_urls", "") or ""
    urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    primary = (settings.dhis2_base_url or "").strip().rstrip("/")
    if not urls:
        urls = [primary] if primary else []
    # De-duplicate, keep order.
    seen: set[str] = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def snapshot_mode(settings) -> str:
    mode = (getattr(settings, "dhis2_snapshot_mode", "auto") or "auto").strip().lower()
    return mode if mode in {"auto", "off", "always"} else "auto"


def snapshot_path(settings) -> Path:
    raw = getattr(settings, "dhis2_snapshot_path", "") or ""
    return Path(raw) if raw else DEFAULT_SNAPSHOT_PATH


# ---------------------------------------------------------------------------
# Canonical request keys
# ---------------------------------------------------------------------------

def _canon_value(key: str, value: Any, aliases: dict[str, str] | None = None) -> str:
    value = str(value)
    if key in {"dimension", "filter"} and ":" in value:
        name, _, items = value.partition(":")
        parts = [i for i in items.split(";") if i]
        if aliases and name == "ou":
            parts = [aliases.get(i, i) for i in parts]
        return name + ":" + ";".join(sorted(set(parts)))
    return value


def canonical_key(
    path: str,
    params: dict[str, Any] | None,
    relaxed: bool = False,
    aliases: dict[str, str] | None = None,
) -> str:
    """Order-independent key for a DHIS2 GET.

    ``relaxed`` keeps only the analytics dimension/filter params so a request
    that differs in display options still finds the recorded response, and
    applies ``aliases`` to org-unit items (the service account's
    ``USER_ORGUNIT`` is the country root, so both spellings are one request).
    """
    path = "/" + path.lstrip("/")
    pairs: list[tuple[str, str]] = []
    for k, v in (params or {}).items():
        if relaxed and k not in {"dimension", "filter"}:
            continue
        values = v if isinstance(v, (list, tuple)) else [v]
        for item in values:
            pairs.append((k, _canon_value(k, item, aliases if relaxed else None)))
    pairs.sort()
    return path + ("?" + urlencode(pairs) if pairs else "")


# ---------------------------------------------------------------------------
# Snapshot store (read) and recorder (write)
# ---------------------------------------------------------------------------

class SnapshotStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded = False
        self.meta: dict[str, Any] = {}
        self.entries: dict[str, Any] = {}
        self.relaxed: dict[str, str] = {}

    def load(self) -> "SnapshotStore":
        if self._loaded:
            return self
        self._loaded = True
        if not self.path.exists():
            logger.warning("dhis2_snapshot_missing", extra={"path": str(self.path)})
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("dhis2_snapshot_unreadable", extra={"path": str(self.path), "error": str(exc)})
            return self
        self.meta = data.get("meta", {})
        self.entries = data.get("entries", {})
        aliases = self.meta.get("ou_aliases") or {}
        for key in self.entries:
            if key.startswith("/api/analytics"):
                path, _, query = key.partition("?")
                # Rebuild the relaxed key from the stored canonical query.
                from urllib.parse import parse_qsl

                params: dict[str, list[str]] = {}
                for k, v in parse_qsl(query, keep_blank_values=True):
                    params.setdefault(k, []).append(v)
                self.relaxed.setdefault(canonical_key(path, params, relaxed=True, aliases=aliases), key)
        logger.info("dhis2_snapshot_loaded", extra={"entries": len(self.entries), "recorded_at": self.meta.get("recorded_at")})
        return self

    @property
    def available(self) -> bool:
        return bool(self.load().entries)

    @property
    def recorded_at(self) -> str | None:
        return self.load().meta.get("recorded_at")

    def lookup(self, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self.load()
        key = canonical_key(path, params)
        if key in self.entries:
            return self.entries[key]
        if path.lstrip("/").startswith("api/analytics"):
            rk = canonical_key(path, params, relaxed=True, aliases=self.meta.get("ou_aliases") or {})
            if rk in self.relaxed:
                return self.entries[self.relaxed[rk]]
        raise SnapshotMiss(
            "DHIS2 is unreachable and this question is not in the offline demo snapshot. "
            "Try one of the suggested questions."
        )


class SnapshotRecorder:
    """Appends every successful live response to a snapshot file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"meta": {}, "entries": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.data.setdefault("entries", {})
        self.data.setdefault("meta", {})

    def record(self, base_url: str, path: str, params: dict[str, Any] | None, payload: dict[str, Any]) -> None:
        with self._lock:
            self.data["entries"][canonical_key(path, params)] = payload
            self.data["meta"].update({
                "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "source_base_url": base_url,
            })
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)

    def set_meta(self, **kw: Any) -> None:
        with self._lock:
            self.data["meta"].update(kw)


_stores: dict[str, SnapshotStore] = {}
_recorders: dict[str, SnapshotRecorder] = {}


def get_store(settings) -> SnapshotStore:
    p = str(snapshot_path(settings))
    if p not in _stores:
        _stores[p] = SnapshotStore(Path(p))
    return _stores[p]


def get_recorder(settings) -> SnapshotRecorder | None:
    target = getattr(settings, "dhis2_snapshot_record_path", "") or ""
    if not target:
        return None
    if target not in _recorders:
        _recorders[target] = SnapshotRecorder(Path(target))
    return _recorders[target]


# ---------------------------------------------------------------------------
# Process-wide status + health probing
# ---------------------------------------------------------------------------

_status: dict[str, Any] = {
    "mode": "unknown",     # live | fallback | cached | down | unknown
    "base_url": None,
    "checked_at": None,
    "instances": [],
}
_probe_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _probe_lock
    if _probe_lock is None:
        _probe_lock = asyncio.Lock()
    return _probe_lock


def reset_state() -> None:
    """Test helper: forget probe results and loaded snapshots."""
    global _probe_lock
    _status.update({"mode": "unknown", "base_url": None, "checked_at": None, "instances": []})
    _stores.clear()
    _recorders.clear()
    _probe_lock = None


def snapshot_label(settings) -> str:
    date = get_store(settings).recorded_at or "unknown date"
    try:
        date = datetime.strptime(date, "%Y-%m-%d").strftime("%-d %b %Y")
    except Exception:
        pass
    return f"Cached data as of {date}"


def status(settings) -> dict[str, Any]:
    """Public, secret-free status for the header dot and /health."""
    mode = _status["mode"]
    smode = snapshot_mode(settings)
    if smode == "always":
        mode = "cached"
    out = {
        "mode": mode,
        "base_url": _status["base_url"] if mode in {"live", "fallback"} else None,
        "checked_at": _status["checked_at"],
        "snapshot_mode": smode,
        "snapshot_available": get_store(settings).available,
        "snapshot_recorded_at": get_store(settings).recorded_at,
        "instances": [
            {k: v for k, v in i.items() if k in {"url", "ok", "version", "ms"}}
            for i in _status["instances"]
        ],
    }
    if mode == "cached":
        out["label"] = snapshot_label(settings)
    elif mode == "fallback":
        out["label"] = f"Fallback DHIS2 instance: {_status['base_url']}"
    elif mode == "live":
        out["label"] = f"Live DHIS2: {_status['base_url']}"
    elif mode == "down":
        out["label"] = "DHIS2 unreachable"
    else:
        out["label"] = "DHIS2 status not checked yet"
    return out


async def _probe_one(settings, url: str, timeout: float) -> dict[str, Any]:
    auth = None
    if settings.dhis2_service_account_user and settings.dhis2_service_account_pass:
        auth = (settings.dhis2_service_account_user, settings.dhis2_service_account_pass)
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), auth=auth, follow_redirects=True) as c:
            r = await c.get(f"{url}/api/system/info.json", headers={"Accept": "application/json"})
        ok = r.status_code == 200 and "json" in r.headers.get("content-type", "") and bool(r.json().get("version"))
        version = r.json().get("version") if ok else None
        return {"url": url, "ok": ok, "version": version, "status": r.status_code,
                "ms": int((time.monotonic() - start) * 1000)}
    except Exception as exc:
        return {"url": url, "ok": False, "version": None, "error": type(exc).__name__,
                "ms": int((time.monotonic() - start) * 1000)}


async def probe(settings, force: bool = True) -> dict[str, Any]:
    """Health-check every configured instance concurrently and pick a source."""
    async with _lock():
        urls = base_urls(settings)
        timeout = float(getattr(settings, "dhis2_health_timeout_seconds", 8) or 8)
        results = await asyncio.gather(*[_probe_one(settings, u, timeout) for u in urls]) if urls else []
        chosen = next((r for r in results if r["ok"]), None)
        smode = snapshot_mode(settings)
        if smode == "always":
            mode, base = "cached", None
        elif chosen:
            mode = "live" if chosen["url"] == urls[0] else "fallback"
            base = chosen["url"]
        elif smode == "auto" and get_store(settings).available:
            mode, base = "cached", None
        else:
            mode, base = "down", (urls[0] if urls else None)
        previous = _status["mode"]
        _status.update({
            "mode": mode,
            "base_url": base,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "instances": list(results),
        })
        if previous != mode:
            logger.warning("dhis2_source_changed", extra={"from": previous, "to": mode, "base_url": base})
        return status(settings)


def current_target(settings) -> tuple[str, str]:
    """(mode, base_url) to use for the next request, without probing.

    Before the first probe we optimistically use the primary instance.
    """
    if snapshot_mode(settings) == "always":
        return "cached", ""
    mode = _status["mode"]
    if mode in {"live", "fallback"} and _status["base_url"]:
        return mode, _status["base_url"]
    if mode == "cached":
        return "cached", ""
    urls = base_urls(settings)
    return "live", (urls[0] if urls else settings.dhis2_base_url.rstrip("/"))


# ---------------------------------------------------------------------------
# Per-request tracker (which source served THIS answer)
# ---------------------------------------------------------------------------

_request_source: contextvars.ContextVar[dict | None] = contextvars.ContextVar("dhis2_request_source", default=None)


def begin_request() -> dict:
    tracker: dict[str, Any] = {"mode": None, "base_url": None}
    _request_source.set(tracker)
    return tracker


def note_source(mode: str, base_url: str | None) -> None:
    tracker = _request_source.get()
    if tracker is None:
        return
    if tracker["mode"] is None or MODES_ORDER.get(mode, 0) > MODES_ORDER.get(tracker["mode"], 0):
        tracker["mode"] = mode
        tracker["base_url"] = base_url
