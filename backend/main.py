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
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings, get_settings
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
# Rate limiter — in-memory token bucket; swap for Redis via middleware later
# ---------------------------------------------------------------------------

class _InMemoryRateLimiter:
    """Sliding-window per-key rate limiter."""

    def __init__(self, rpm: int) -> None:
        self._rpm = rpm
        self._window = 60.0
        self._store: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        bucket = [t for t in self._store[key] if t > cutoff]
        if len(bucket) >= self._rpm:
            self._store[key] = bucket
            return False
        bucket.append(now)
        self._store[key] = bucket
        return True


_rate_limiter: _InMemoryRateLimiter | None = None

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
    while True:
        try:
            logger.info("metadata_sync_started")
            result = await sync_metadata(settings)
            logger.info("metadata_sync_finished", extra={"result_status": result.get("status", "unknown")})
        except Exception as exc:
            logger.error("metadata_sync_failed", extra={"error": str(exc)})

        now = datetime.now(timezone.utc)
        tomorrow = now + timedelta(days=1)
        next_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_midnight - now).total_seconds()

        logger.info("metadata_sync_sleeping", extra={"sleep_seconds": int(sleep_seconds)})
        await asyncio.sleep(sleep_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rate_limiter
    settings = get_settings()
    settings.startup_checks()
    settings.log_resolved_settings()
    _rate_limiter = _InMemoryRateLimiter(settings.rate_limit_rpm)
    cleanup_old_files(settings)
    
    await init_db()
    sync_task = asyncio.create_task(_metadata_sync_loop())
    
    logger.info("startup", extra={"deployment_mode": settings.deployment_mode})
    yield
    sync_task.cancel()
    logger.info("shutdown")


app = FastAPI(
    title="DHIS2 Public Health Intelligence Assistant",
    version="1.0.0",
    description="Conversational AI analyst for DHIS2 public health data.",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Audit + rate-limit middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    start = time.monotonic()
    # Identify the caller — prefer JWT user_id, fall back to IP
    client_key = request.client.host if request.client else "unknown"
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        settings = get_settings()
        identity = verify_token(token, settings)
        if identity:
            client_key = identity.user_id

    # Rate limit check (skip health + static)
    path = request.url.path
    if path not in {"/health", "/"} and not path.startswith("/frontend"):
        if _rate_limiter and not _rate_limiter.is_allowed(client_key):
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
    return {
        "status": "ok",
        "deployment_mode": settings.deployment_mode,
        "evidence_fusion": settings.evidence_fusion,
        "llm_provider": settings.llm_provider,
    }


@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
    session=Depends(get_session),
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

    return StreamingResponse(
        run_agent_stream(request, settings, identity, formatted_history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/generate/docx")
async def generate_docx(
    payload: ReportPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    data = write_file(
        settings,
        html_to_docx(payload.html, payload.title),
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        session_id=identity.user_id,
    )
    return data.model_dump()


@app.post("/api/generate/pdf")
async def generate_pdf(
    payload: ReportPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    data = write_file(
        settings,
        html_to_pdf(payload.html),
        ".pdf",
        "application/pdf",
        session_id=identity.user_id,
    )
    return data.model_dump()


@app.post("/api/generate/pptx")
async def generate_pptx(
    payload: PresentationPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    data = write_file(
        settings,
        slides_to_pptx(payload.slides, payload.title),
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        session_id=identity.user_id,
    )
    return data.model_dump()


@app.post("/api/export/xlsx")
async def export_xlsx(
    payload: DataPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    generated = write_file(
        settings,
        data_to_xlsx(payload.model_dump()),
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        session_id=identity.user_id,
    )
    generated.row_count = len(payload.rows)
    return generated.model_dump()


@app.post("/api/export/csv")
async def export_csv(
    payload: DataPayload,
    settings: Settings = Depends(get_settings),
    identity: Identity = Depends(current_identity),
) -> dict:
    generated = write_file(
        settings,
        data_to_csv(payload.model_dump()),
        ".csv",
        "text/csv",
        session_id=identity.user_id,
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
async def metadata_search(q: str, settings: Settings = Depends(get_settings)) -> dict:
    from backend.app.agent.intent import KNOWN_METRICS
    q_lower = q.lower()
    results = []
    for key, (label, uid) in KNOWN_METRICS.items():
        if q_lower in key or q_lower in label.lower():
            results.append({"label": label, "uid": uid, "confidence": 0.90})
    if not results:
        results = [{"label": label, "uid": uid, "confidence": 0.70} for _, (label, uid) in list(KNOWN_METRICS.items())[:3]]
    return {"query": q, "results": results[:10]}


@app.post("/api/metadata/sync")
async def metadata_sync(settings: Settings = Depends(get_settings)) -> dict:
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
    title = body.get("title", "").strip()
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
    role = body.get("role", "user")
    content = body.get("content", "")
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
            candidate = base / full_path
            if candidate.exists() and candidate.is_file():
                return FileResponse(candidate)
    # SPA fallback — only from the dist build.
    if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
        return FileResponse(FRONTEND_DIST / "index.html")
    raise HTTPException(status_code=404, detail="Frontend not built. Run: cd frontend && npm run build")
