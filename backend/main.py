"""FastAPI application entrypoint.

Features:
- Per-user rate limiting (in-memory; Redis when REDIS_URL is set)
- Structured audit logging middleware (no secrets logged)
- Lifespan context manager (replaces deprecated @app.on_event)
- Serves frontend/dist/ in production; frontend/ when dist/ not present
- All API routes with proper auth injection
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings, get_settings
from backend.app.ratelimit import RateLimiter, create_rate_limiter
from backend.app.agent.graph import run_agent_stream
from backend.app.db.session import init_db, get_session
from backend.app.db import conversations as conv_db
from backend.app.auth.dhis2_adapter import validate_dhis2_token
from backend.app.auth.standalone_adapter import issue_token, verify_token
from backend.app.dhis2.metadata_sync import sync_metadata
from backend.app.generators.docx_gen import html_to_docx
from backend.app.generators.file_store import cleanup_old_files, resolve_file, write_file
from backend.app.generators.pdf_gen import html_to_pdf
from backend.app.generators.pptx_gen import slides_to_pptx
from backend.app.generators.xlsx_gen import data_to_csv, data_to_xlsx
from backend.app.models import (
    DataPayload,
    Identity,
    LoginRequest,
    PresentationPayload,
    ReportPayload,
    ChatRequest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("dhis2_analyst")

# ---------------------------------------------------------------------------
# Rate limiter — Redis-backed when REDIS_URL is set, else in-memory (see
# backend/app/ratelimit.py).
# ---------------------------------------------------------------------------

_rate_limiter: RateLimiter | None = None

# Paths exempt from rate limiting: health check, SPA entry, and static assets
# (SPA loads issue several of these per page; counting them starves real users
# behind a shared NAT IP). API/auth routes are always limited.
_RATE_LIMIT_EXEMPT_EXACT = {"/health", "/", "/api/dhis2/status"}
_RATE_LIMIT_EXEMPT_PREFIXES = ("/frontend", "/assets")
_RATE_LIMIT_EXEMPT_EXTS = (
    ".js", ".css", ".svg", ".ico", ".png", ".woff2", ".woff", ".ttf", ".map",
)


def _client_key_for(request: Request, settings: Settings) -> str:
    """Resolve the rate-limit key: JWT user_id when present, else client IP."""
    auth_header = request.headers.get("authorization", "")
    if auth_header[:7].lower() == "bearer ":
        identity = verify_token(auth_header[7:], settings)
        if identity:
            return identity.user_id

    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_rate_limit_exempt(request: Request) -> bool:
    if request.method == "OPTIONS":  # CORS preflight — never charge a token
        return True
    path = request.url.path
    if path in _RATE_LIMIT_EXEMPT_EXACT:
        return True
    if path.startswith(_RATE_LIMIT_EXEMPT_PREFIXES):
        return True
    return path.endswith(_RATE_LIMIT_EXEMPT_EXTS)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

async def _metadata_sync_loop():
    """Background task to sync metadata on startup and then every midnight UTC.

    Delays initial sync by 3 seconds to let the DB engine fully initialise
    and prevent SQLite lock contention during startup.
    """
    settings = get_settings()
    # Let the server finish startup before the first sync attempt.
    await asyncio.sleep(3)
    # Retry backoff schedule (seconds) used when a sync fails or indexes nothing,
    # so a transient DHIS2 outage at startup doesn't leave resolution broken
    # until the next midnight.
    backoff = [60, 300, 1800]
    consecutive_failures = 0
    while True:
        status = "unknown"
        try:
            logger.info("metadata_sync_started")
            result = await sync_metadata(settings)
            status = result.get("status", "unknown")
            logger.info("metadata_sync_finished", extra={"result_status": status})
        except Exception as exc:
            logger.error("metadata_sync_failed", extra={"error": str(exc)})
            status = "failed"

        if status in {"failed", "partial"}:
            delay = backoff[min(consecutive_failures, len(backoff) - 1)]
            consecutive_failures += 1
            logger.warning(
                "metadata_sync_retry_scheduled",
                extra={"status": status, "retry_in_seconds": delay, "attempt": consecutive_failures},
            )
            await asyncio.sleep(delay)
            continue

        consecutive_failures = 0
        now = datetime.now(timezone.utc)
        tomorrow = now + timedelta(days=1)
        next_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_midnight - now).total_seconds()

        logger.info("metadata_sync_sleeping", extra={"sleep_seconds": int(sleep_seconds)})
        await asyncio.sleep(sleep_seconds)


async def _temp_file_cleanup_loop():
    """Periodically sweep expired generated files.

    Startup-only cleanup lets files accumulate unboundedly on a long-running
    server (each export up to max_file_size_bytes) until the temp dir fills and
    every export starts failing with ENOSPC.
    """
    settings = get_settings()
    interval = max(300, settings.max_file_age_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(cleanup_old_files, settings)
        except Exception as exc:
            logger.warning("temp_file_cleanup_failed", extra={"error": str(exc)})


async def _dhis2_health_loop():
    """Re-check DHIS2 instances periodically so the app moves back to the
    primary as soon as it recovers (and to fallback/cached when it drops)."""
    settings = get_settings()
    if not settings.dhis2_service_account_user:
        return
    from backend.app.dhis2 import failover

    interval = max(15, settings.dhis2_health_interval_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            await failover.probe(settings)
        except Exception as exc:
            logger.warning("dhis2_health_probe_error", extra={"error": str(exc)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rate_limiter
    settings = get_settings()
    settings.startup_checks()
    settings.log_resolved_settings()
    _rate_limiter = await create_rate_limiter(settings)
    cleanup_old_files(settings)
    
    await init_db()

    # --- DHIS2 connectivity check + source selection (live/fallback/cached) ---
    if settings.dhis2_service_account_user:
        from backend.app.dhis2 import failover
        try:
            st = await failover.probe(settings)
            logger.info(
                "dhis2_source_selected",
                extra={"mode": st["mode"], "base_url": st["base_url"], "instances": st["instances"]},
            )
            if st["mode"] == "down":
                logger.warning(
                    "dhis2_health_failed: no DHIS2 instance is reachable and the demo "
                    "snapshot is disabled or missing — data queries will fail."
                )
        except Exception as exc:
            logger.warning("dhis2_health_probe_error", extra={"error": str(exc)})

    # --- First line of defense: prime metadata index at startup ---
    if settings.dhis2_service_account_user:
        try:
            logger.info("startup_metadata_sync_initiating")
            sync_res = await sync_metadata(settings)
            logger.info("startup_metadata_sync_completed", extra={"status": sync_res.get("status"), "synced": sync_res.get("synced")})
        except Exception as exc:
            logger.warning("startup_metadata_sync_error", extra={"error": str(exc)})

    sync_task = asyncio.create_task(_metadata_sync_loop())
    cleanup_task = asyncio.create_task(_temp_file_cleanup_loop())
    health_task = asyncio.create_task(_dhis2_health_loop())

    logger.info("startup", extra={"deployment_mode": settings.deployment_mode})
    yield
    for task in (sync_task, cleanup_task, health_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if _rate_limiter is not None:
        await _rate_limiter.aclose()
    logger.info("shutdown")


app = FastAPI(
    title="DHIS2 Public Health Intelligence Assistant",
    version="1.0.0",
    description="Conversational AI analyst for DHIS2 public health data.",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Audit + rate-limit middleware
#
# Registered BEFORE CORS so that CORS ends up the OUTERMOST layer (Starlette
# applies the last-added middleware first). That guarantees a 429 emitted here
# still passes back through CORS and carries Access-Control-Allow-Origin, and
# that OPTIONS preflights are answered by CORS before they can consume a token.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    start = time.monotonic()
    settings = get_settings()
    path = request.url.path
    client_key = _client_key_for(request, settings)

    if _rate_limiter and not _is_rate_limit_exempt(request):
        if not await _rate_limiter.is_allowed(client_key):
            logger.warning("rate_limit_exceeded", extra={"client": client_key, "path": path})
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please wait and retry."},
            )

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "request_unhandled_error",
            extra={"method": request.method, "path": path, "error": str(exc), "duration_ms": duration_ms, "client": client_key},
            exc_info=True
        )
        raise

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "client": client_key,
        },
    )
    return response


# ---------------------------------------------------------------------------
# CORS — added last so it is the outermost middleware (see note above).
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Static frontend — serves dist/ (Vite build) or raw frontend/ as fallback
# ---------------------------------------------------------------------------

FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend"
FRONTEND_DIST = FRONTEND_ROOT / "dist"
STATIC_DIR = FRONTEND_DIST if FRONTEND_DIST.exists() else FRONTEND_ROOT

ASSETS_DIR = STATIC_DIR / "assets"
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/", include_in_schema=False)
async def index():
    for candidate in [FRONTEND_DIST / "index.html", FRONTEND_ROOT / "index.html"]:
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="Frontend not built. Run: cd frontend && npm run build")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def current_identity(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Identity:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            # Try standalone JWT first
            identity = verify_token(token, settings)
            if identity:
                return identity
            # Try DHIS2 token validation when in dhis2 or combined mode
            if settings.deployment_mode in {"dhis2", "combined"}:
                dhis2_identity = await validate_dhis2_token(token, settings)
                if dhis2_identity:
                    return dhis2_identity
    raise HTTPException(status_code=401, detail="Authentication required")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict:
    """Liveness for Render/Docker. Always 200 while the process serves
    requests; reports the last known DHIS2 source without probing."""
    from backend.app.dhis2 import failover

    st = failover.status(settings)
    return {
        "status": "ok",
        "deployment_mode": settings.deployment_mode,
        "evidence_fusion": settings.evidence_fusion,
        "llm_provider": settings.llm_provider,
        "dhis2": {"mode": st["mode"], "snapshot_mode": st["snapshot_mode"]},
    }


_DEMO_QUESTIONS_FILE = Path(__file__).resolve().parent / "demo_snapshot" / "demo_questions.json"


@app.get("/api/demo/questions")
async def demo_questions(identity: Identity = Depends(current_identity)) -> list[dict]:
    """Suggested-question chips. Clicking one sends the question through the
    normal agent pipeline (nothing is pre-answered)."""
    import json as _json

    try:
        return _json.loads(_DEMO_QUESTIONS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("demo_questions_unavailable", extra={"error": str(exc)})
        return []


@app.get("/api/dhis2/status")
async def dhis2_status(settings: Settings = Depends(get_settings)) -> dict:
    """Source behind the header status dot: live | fallback | cached | down.

    Public on purpose (the login screen can show it too); contains only the
    public instance URLs and the snapshot date, never credentials.
    """
    from backend.app.dhis2 import failover

    return failover.status(settings)


@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
    authorization: str | None = Header(default=None),
):
    logger.info(
        "chat_request",
        extra={
            "session_id": request.session_id,
            "user_id": identity.user_id,
            "role": identity.role,
            "mode": request.output_mode,
            "allow_web": request.allow_web,
        },
    )
    
    history = []
    if request.conversation_id:
        try:
            history = await conv_db.get_messages(session, request.conversation_id, identity.user_id)
        except Exception as exc:
            logger.warning("failed_to_load_chat_history", extra={"error": str(exc)})

    previous_messages = []
    if history:
        if history[-1]["role"] == "user":
            previous_messages = history[:-1]
        else:
            previous_messages = history

    formatted_history = []
    for msg in previous_messages[-6:]:
        content = msg["content"]
        # Cap assistant responses — intent classifier needs topic context, not full reports
        if msg["role"] == "assistant" and len(content) > 500:
            content = content[:500] + "…"
        formatted_history.append({
            "role": msg["role"],
            "content": content
        })

    # For DHIS2-authenticated users, thread the raw PAT into the agent so
    # fetch_dhis2 makes real per-user API calls instead of falling back to mock.
    dhis2_token = None
    if identity.role == "dhis2_user" and authorization:
        scheme, _, tok = authorization.partition(" ")
        if scheme.lower() in {"bearer", "apitoken"} and tok:
            dhis2_token = tok

    async def _safe_stream():
        """Guarantee the SSE stream always terminates with error+done.

        run_agent_stream guards its named nodes, but an exception raised
        between/inside yields (serialization, edge-case rendering) would
        otherwise abort the chunked response with no event, leaving the
        frontend stuck on an empty assistant bubble.
        """
        from backend.app.agent.graph import sse

        try:
            async for chunk in run_agent_stream(request, settings, identity, formatted_history, dhis2_token):
                yield chunk
        except Exception as exc:  # pragma: no cover - defensive backstop
            logger.error("chat_stream_unhandled_error", extra={"error": str(exc)}, exc_info=True)
            yield sse("error", {
                "code": "STREAM_FAILED",
                "user_message": "Something went wrong while generating the response. Please try again.",
                "detail": str(exc),
            })
            yield sse("done")

    return StreamingResponse(
        _safe_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _generate_and_store(settings, identity, produce, suffix, content_type):
    """Run a blocking generator + file write in a worker thread.

    WeasyPrint/python-docx/openpyxl/python-pptx are synchronous and CPU-bound
    (seconds for a large report). Running them inline on the event loop freezes
    every concurrent SSE chat stream; offloading keeps the loop responsive.
    """
    def _work():
        return write_file(settings, produce(), suffix, content_type, session_id=identity.user_id)

    return await asyncio.to_thread(_work)


@app.post("/api/generate/docx")
async def generate_docx(
    payload: ReportPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    data = await _generate_and_store(
        settings, identity,
        lambda: html_to_docx(payload.html, payload.title),
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    return data.model_dump()


@app.post("/api/generate/pdf")
async def generate_pdf(
    payload: ReportPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    data = await _generate_and_store(
        settings, identity,
        lambda: html_to_pdf(payload.html),
        ".pdf",
        "application/pdf",
    )
    return data.model_dump()


@app.post("/api/generate/pptx")
async def generate_pptx(
    payload: PresentationPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    data = await _generate_and_store(
        settings, identity,
        lambda: slides_to_pptx(payload.slides, payload.title),
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    return data.model_dump()


@app.post("/api/export/xlsx")
async def export_xlsx(
    payload: DataPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    generated = await _generate_and_store(
        settings, identity,
        lambda: data_to_xlsx(payload.model_dump()),
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    generated.row_count = len(payload.rows)
    return generated.model_dump()


@app.post("/api/export/csv")
async def export_csv(
    payload: DataPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    generated = await _generate_and_store(
        settings, identity,
        lambda: data_to_csv(payload.model_dump()),
        ".csv",
        "text/csv",
    )
    generated.row_count = len(payload.rows)
    return generated.model_dump()


@app.get("/api/download/{file_id}")
async def download(
    file_id: str,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
):
    path = resolve_file(settings, file_id, session_id=identity.user_id)
    if not path:
        raise HTTPException(status_code=404, detail="File not found or expired.")
    return FileResponse(path, filename=path.name)


@app.get("/api/metadata/search")
async def metadata_search(
    q: str,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    """Search the synced metadata index for indicators matching ``q``."""
    from backend.app.db.metadata_index import search_indicator_by_name
    from backend.app.db.session import get_db_session

    if not q or not q.strip():
        return {"query": q, "results": []}

    try:
        async with get_db_session() as session:
            matches = await search_indicator_by_name(
                session, q.strip(), is_postgres=settings.is_postgres, limit=10
            )
    except Exception as exc:
        logger.warning("metadata_search_failed", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail="Metadata index is unavailable.")

    results = [
        {"label": m["label"], "uid": m["uid"], "confidence": m["uid_confidence"]}
        for m in matches
    ]
    return {"query": q, "results": results}


@app.post("/api/metadata/sync")
async def metadata_sync(
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    # Heavy DHIS2 fan-out + bulk DB writes — must not be anonymously triggerable.
    if identity.role not in {"admin", "dhis2_user"}:
        raise HTTPException(status_code=403, detail="Not permitted to trigger metadata sync.")
    return await sync_metadata(settings)


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

@app.get("/api/conversations")
async def list_conversations(
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
) -> list[dict]:
    return await conv_db.list_conversations(session, identity.user_id)


@app.post("/api/conversations")
async def create_conversation(
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
) -> dict:
    return await conv_db.create_conversation(session, identity.user_id)


@app.get("/api/conversations/{conv_id}/messages")
async def get_conversation_messages(
    conv_id: str,
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
) -> list[dict]:
    conv = await conv_db.get_conversation(session, conv_id, identity.user_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await conv_db.get_messages(session, conv_id, identity.user_id)


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
) -> dict:
    deleted = await conv_db.delete_conversation(session, conv_id, identity.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


@app.patch("/api/conversations/{conv_id}")
async def rename_conversation(
    conv_id: str,
    body: dict,
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
) -> dict:
    raw_title = body.get("title", "")
    if not isinstance(raw_title, str):
        raise HTTPException(status_code=422, detail="title must be a string")
    title = raw_title.strip()[:200]
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    updated = await conv_db.update_title(session, conv_id, identity.user_id, title)
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"updated": True}


@app.post("/api/conversations/{conv_id}/messages")
async def add_message(
    conv_id: str,
    body: dict,
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
) -> dict:
    conv = await conv_db.get_conversation(session, conv_id, identity.user_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Only user/assistant turns may be stored — they are replayed verbatim into
    # the intent LLM prompt, so an arbitrary "system"/"tool" role would let a
    # client inject instructions (or break the provider call → silent degrade).
    role = body.get("role", "user")
    if role not in {"user", "assistant"}:
        raise HTTPException(status_code=422, detail="role must be 'user' or 'assistant'")
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=422, detail="content must be a string")
    content = content[:16000]
    artefacts = body.get("artefacts")
    msg = await conv_db.add_message(session, conv_id, role, content, artefacts)
    return msg


@app.post("/auth/login")
async def login(request: LoginRequest, settings: Settings = Depends(get_settings)) -> dict:
    if settings.jwt_secret == "change-me-in-production":
        logger.warning("insecure_jwt_secret_in_use — set JWT_SECRET in .env for production")

    # Production deployments: validate against an admin-managed stakeholder
    # directory. For initial deployment the username/password is accepted and
    # the identity is scoped to external_stakeholder with no org unit grants.
    # Replace this block with your credential store validation.
    if not request.username or not request.password:
        raise HTTPException(status_code=422, detail="username and password are required")

    if settings.is_hosted and not settings.standalone_access_code.strip():
        # Startup refuses this config; this is a belt-and-braces guard.
        raise HTTPException(status_code=503, detail="Login is disabled: access code not configured.")

    if settings.standalone_access_code and not hmac.compare_digest(
        request.password.encode(), settings.standalone_access_code.encode()
    ):
        logger.info("login_rejected", extra={"user_id": request.username})
        raise HTTPException(status_code=401, detail="Invalid username or password")

    identity = Identity(
        user_id=request.username,
        role="external_stakeholder",
        permitted_org_units=[],
        permitted_indicator_groups=[],
    )
    token = issue_token(identity, settings)
    logger.info("login_ok", extra={"user_id": request.username})
    return {"access_token": token, "token_type": "bearer", "identity": identity.model_dump()}


@app.get("/auth/me")
async def me(identity: Identity = Depends(current_identity)) -> dict:
    return identity.model_dump()


# Serve any static path from the frontend build (needed for Vite asset hashes).
# Keep this after API/auth routes so unknown API paths return real 404s.
_STATIC_EXTS = {".js", ".css", ".svg", ".ico", ".png", ".woff2", ".woff", ".ttf", ".map", ".json"}
# Raw source files that should never be served directly (causes blank page).
_SOURCE_EXTS = {".jsx", ".tsx", ".ts"}


@app.get("/{full_path:path}", include_in_schema=False)
async def catch_all(full_path: str):
    if full_path.startswith(("api/", "auth/")):
        raise HTTPException(status_code=404, detail="Not found")
    # Never serve raw source files — browser cannot parse JSX/TS.
    if any(full_path.endswith(ext) for ext in _SOURCE_EXTS):
        logger.debug("catch_all_rejected_source_file", extra={"path": full_path})
        raise HTTPException(status_code=404, detail="Source file not served. Build the frontend first.")
    if any(full_path.endswith(ext) for ext in _STATIC_EXTS):
        for base in [FRONTEND_DIST, FRONTEND_ROOT]:
            base_resolved = base.resolve()
            candidate = (base / full_path).resolve()
            # Contain within the frontend tree — percent-decoded '../' segments
            # must not escape it and serve arbitrary files from disk.
            if not candidate.is_relative_to(base_resolved):
                logger.warning("catch_all_path_traversal_blocked", extra={"path": full_path})
                continue
            if candidate.exists() and candidate.is_file():
                return FileResponse(candidate)
    # SPA fallback — only from the dist build.
    if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
        return FileResponse(FRONTEND_DIST / "index.html")
    raise HTTPException(status_code=404, detail="Frontend not built. Run: cd frontend && npm run build")
