"""LiteLLM-backed LLM adapter.

Routes all requests through litellm.acompletion(). Requires a configured
LLM provider (openai, anthropic, ollama, azure, etc.) and appropriate
credentials (LLM_API_KEY or LLM_BASE_URL).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.config import Settings

logger = logging.getLogger("dhis2_analyst.llm")

# Per-request LLM call log (purpose, model, seconds, transient errors), exposed
# to the client in the final "timings" SSE event so latency can be diagnosed
# without server log access. No prompts, keys or content are recorded.
import contextvars

_CALL_LOG: contextvars.ContextVar[list | None] = contextvars.ContextVar("llm_call_log", default=None)


def begin_call_log() -> list:
    log: list = []
    _CALL_LOG.set(log)
    return log


def _record_call(entry: dict) -> None:
    log = _CALL_LOG.get()
    if log is not None:
        log.append(entry)

_TRANSIENT_MARKERS = (
    "503", "unavailable", "high demand", "overloaded", "429", "resource_exhausted",
    "rate limit", "ratelimit", "timeout", "timed out", "500", "internal error",
    "502", "504", "apiconnectionerror", "serviceunavailable",
)


_PRIMARY_DOWN_UNTIL: dict[str, float] = {}
_BREAKER_SECONDS = 180.0
_QUOTA_BREAKER_SECONDS = 900.0


def _is_quota_exhausted(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return ("429" in text or "ratelimit" in text or "resource_exhausted" in text or "quota" in text) and (
        "per day" in text or "perday" in text or "quota" in text or "resource_exhausted" in text
    )


def _safe_error(exc: Exception) -> str:
    """Short, key-free error summary for the client-side timings event."""
    import re as _re
    msg = f"{type(exc).__name__}: {exc}"
    msg = _re.sub(r"(key=|api[_-]?key[\"':= ]+)[A-Za-z0-9_\-]{8,}", r"\1***", msg, flags=_re.I)
    msg = _re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "***", msg)
    return msg[:220]


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def _fallback_models(settings: Settings, primary: str) -> list[str]:
    raw = (settings.llm_fallback_models or "").strip()
    if raw.lower() == "none":
        return []
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
    elif "gemini" in settings.llm_provider.lower() or "gemini" in primary.lower():
        models = ["gemini/gemini-flash-lite-latest"]
    else:
        models = []
    return [m for m in models if m != primary]


async def complete(
    messages: list[dict[str, str]],
    settings: Settings,
    *,
    json_mode: bool = False,
    temperature: float = 0.3,
) -> str:
    """Call the configured LLM provider and return the assistant message string.

    Args:
        messages:    OpenAI-format message list, e.g. [{"role": "user", "content": "..."}]
        settings:    Application settings.
        json_mode:   Request a JSON response object (provider must support it).
        temperature: Sampling temperature; lower = more deterministic.

    Returns:
        The assistant's response as a string.

    Raises:
        RuntimeError: If the LLM call fails and provider is not mock.
    """
    if settings.llm_provider == "mock":
        return _mock_completion(messages, json_mode=json_mode)

    try:
        import os as _os
        # Use the bundled model cost map instead of fetching it from GitHub on
        # first import (slow on a cold free-tier instance).
        _os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        # Let LiteLLM drop provider-unsupported params (e.g. response_format on
        # models without JSON mode) instead of raising — otherwise every intent
        # call fails on such providers and the pipeline silently degrades to the
        # keyword classifier. The markdown-fence JSON cleanup path handles the
        # non-strict output.
        litellm.drop_params = True

        model = _resolve_model(settings)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": settings.llm_timeout_seconds,
            "num_retries": 0,  # retries/fallbacks handled below
        }

        if "gemini" not in settings.llm_provider.lower() and "gemini" not in model.lower():
            kwargs["temperature"] = temperature

        if settings.llm_base_url:
            kwargs["api_base"] = settings.llm_base_url
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        logger.info(
            "llm_call",
            extra={
                "provider": settings.llm_provider,
                "model": model,
                "json_mode": json_mode,
                "message_count": len(messages),
            },
        )

        start = time.monotonic()
        fallbacks = _fallback_models(settings, model)
        candidates = [model] + fallbacks
        retries = max(0, int(settings.llm_transient_retries))
        # Circuit breaker: right after the primary model exhausted its retries
        # on transient errors (e.g. Gemini 503 "high demand"), go straight to
        # the fallback for a few minutes, keeping the primary as the last resort
        # with a single attempt. Saves ~10-20 s of doomed retries per call.
        breaker_open = bool(fallbacks) and _PRIMARY_DOWN_UNTIL.get(model, 0.0) > time.monotonic()
        if breaker_open:
            candidates = fallbacks + [model]
        last_exc: Exception | None = None
        live = [c for c in candidates if _PRIMARY_DOWN_UNTIL.get(c, 0.0) <= time.monotonic()]
        if live and len(live) < len(candidates):
            # Models marked down go last, with a single attempt.
            candidates = live + [c for c in candidates if c not in live]
        for idx, candidate in enumerate(candidates):
            # Primary gets the full retry budget; each fallback gets one retry.
            is_primary = candidate == model
            attempts = 1 + (retries if (is_primary and not breaker_open) else min(retries, 1))
            if breaker_open and is_primary:
                attempts = 1
            for attempt in range(attempts):
                try:
                    response = await litellm.acompletion(**{**kwargs, "model": candidate})
                except Exception as exc:  # noqa: BLE001 - classified below
                    last_exc = exc
                    if not _is_transient(exc):
                        raise
                    logger.warning(
                        "llm_call_transient",
                        extra={"model": candidate, "attempt": attempt + 1, "error": str(exc)[:300]},
                    )
                    _record_call({"model": candidate, "attempt": attempt + 1, "transient": True,
                                  "error": _safe_error(exc), "seconds": round(time.monotonic() - start, 2)})
                    if _is_quota_exhausted(exc):
                        # Daily / quota exhaustion: retrying the same model only burns
                        # more quota and time. Mark it down and move to the next model.
                        _PRIMARY_DOWN_UNTIL[candidate] = time.monotonic() + _QUOTA_BREAKER_SECONDS
                        break
                    if attempt + 1 < attempts:
                        await asyncio.sleep(settings.llm_retry_backoff_seconds * (2 ** attempt))
                    elif is_primary and fallbacks:
                        _PRIMARY_DOWN_UNTIL[model] = time.monotonic() + _BREAKER_SECONDS
                    continue
                content = response.choices[0].message.content or ""
                _PRIMARY_DOWN_UNTIL.pop(candidate, None)
                _record_call({"model": candidate, "fallback": not is_primary, "breaker": breaker_open, "attempt": attempt + 1,
                              "seconds": round(time.monotonic() - start, 2), "json": json_mode})
                logger.info(
                    "llm_call_ok",
                    extra={
                        "provider": settings.llm_provider,
                        "model": candidate,
                        "fallback": idx > 0,
                        "chars": len(content),
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                )
                return content
        assert last_exc is not None
        _record_call({"model": model, "failed": True, "seconds": round(time.monotonic() - start, 2),
                      "error": type(last_exc).__name__})
        raise last_exc

    except Exception as exc:
        logger.error(
            "llm_call_failed",
            extra={"error": str(exc), "provider": settings.llm_provider, "model": settings.llm_model},
        )
        raise RuntimeError(f"LLM call failed ({settings.llm_provider}): {exc}") from exc


async def embed(text: str, settings: Settings) -> list[float]:
    """Generate a text embedding vector for semantic search.

    Returns an empty list ONLY in mock mode (pgvector disabled). Real provider
    errors are raised so callers can distinguish "embeddings intentionally off"
    from "embedding backend failed" and degrade appropriately instead of
    silently treating a transient outage as an empty result.
    """
    if settings.embedding_provider == "mock":
        return []

    try:
        import litellm

        model = settings.embedding_model
        if settings.embedding_provider not in {"openai"} and "/" not in model:
            model = f"{settings.embedding_provider}/{model}"

        logger.info(
            "embed_call",
            extra={
                "provider": settings.embedding_provider,
                "model": model,
                "text_len": len(text),
            },
        )
        start = time.monotonic()
        response = await litellm.aembedding(
            model=model,
            input=[text],
            api_key=settings.llm_api_key or None,
            api_base=settings.llm_base_url or None,
        )
        vector = response.data[0]["embedding"]
        logger.info(
            "embed_call_ok",
            extra={
                "provider": settings.embedding_provider,
                "dimensions": len(vector),
                "duration_ms": int((time.monotonic() - start) * 1000),
            },
        )
        return vector
    except Exception as exc:
        logger.error("embed_failed", extra={"error": str(exc)})
        raise RuntimeError(f"Embedding call failed ({settings.embedding_provider}): {exc}") from exc


def _mock_completion(messages: list[dict[str, str]], *, json_mode: bool) -> str:
    """Deterministic local response for LLM_PROVIDER=mock.

    Intent classification in mock mode is handled upstream by the deterministic
    classifier, so this only needs to serve content generation (reports and
    conversational replies) with clearly-labelled placeholder text.
    """
    if json_mode:
        return "{}"
    last_user = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user = msg.get("content", "")
            break
    if "HTML" in last_user:
        return (
            "<h1>Report (mock mode)</h1>"
            "<p>This report was generated in mock mode without a live LLM. "
            "Configure a real LLM provider for narrative analysis.</p>"
        )
    return (
        "**Mock mode** — no LLM provider is configured, so I can't generate a "
        "narrative answer. The underlying DHIS2 data is still available in the "
        "dashboard/table above. Set a real `LLM_PROVIDER` to enable full analysis."
    )


def _resolve_model(settings: Settings) -> str:
    """Build the LiteLLM model string from provider + model settings."""
    provider = settings.llm_provider
    model = settings.llm_model

    if provider == "ollama":
        return f"ollama/{model}"
    if provider in {"openai", "azure", "anthropic", "cohere", "gemini"} and "/" not in model:
        return f"{provider}/{model}"
    return model


