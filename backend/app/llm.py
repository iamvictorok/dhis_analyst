"""LiteLLM-backed LLM adapter.

Routes all requests through litellm.acompletion(). Requires a configured
LLM provider (openai, anthropic, ollama, azure, etc.) and appropriate
credentials (LLM_API_KEY or LLM_BASE_URL).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from backend.config import Settings

logger = logging.getLogger("dhis2_analyst.llm")


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
            "num_retries": 1,
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
        response = await litellm.acompletion(**kwargs)
        content = response.choices[0].message.content or ""
        logger.info(
            "llm_call_ok",
            extra={
                "provider": settings.llm_provider,
                "chars": len(content),
                "duration_ms": int((time.monotonic() - start) * 1000),
            },
        )
        return content

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
    if provider in {"openai", "azure", "anthropic", "cohere"} and "/" not in model:
        return f"{provider}/{model}"
    return model


