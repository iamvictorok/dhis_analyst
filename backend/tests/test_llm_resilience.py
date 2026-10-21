"""Transient LLM errors (Gemini 503 'high demand') retry, then fall back."""
import asyncio
import types

import pytest

from backend.app import llm
from backend.config import Settings


def _settings(**kw):
    base = dict(llm_provider="gemini", llm_model="gemini/gemini-flash-latest", llm_api_key="k",
                llm_retry_backoff_seconds=0.0)
    base.update(kw)
    return Settings(**base)


def _resp(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))])


def _patch(monkeypatch, behaviour):
    import litellm
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs["model"])
        out = behaviour(kwargs["model"], len(calls))
        if isinstance(out, Exception):
            raise out
        return _resp(out)

    monkeypatch.setattr(litellm, "acompletion", fake)
    return calls


def test_retries_primary_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, lambda m, n: RuntimeError("503 UNAVAILABLE high demand") if n < 2 else "ok")
    assert asyncio.run(llm.complete([{"role": "user", "content": "x"}], _settings())) == "ok"
    assert calls == ["gemini/gemini-flash-latest"] * 2


def test_falls_back_after_primary_exhausted(monkeypatch):
    def b(m, n):
        return RuntimeError("503 high demand") if m == "gemini/gemini-flash-latest" else "lite"
    calls = _patch(monkeypatch, b)
    assert asyncio.run(llm.complete([{"role": "user", "content": "x"}], _settings())) == "lite"
    assert calls == ["gemini/gemini-flash-latest"] * 3 + ["gemini/gemini-flash-lite-latest"]


def test_non_transient_error_not_retried(monkeypatch):
    calls = _patch(monkeypatch, lambda m, n: RuntimeError("400 invalid argument"))
    with pytest.raises(RuntimeError):
        asyncio.run(llm.complete([{"role": "user", "content": "x"}], _settings()))
    assert calls == ["gemini/gemini-flash-latest"]


def test_fallbacks_can_be_disabled(monkeypatch):
    calls = _patch(monkeypatch, lambda m, n: RuntimeError("503"))
    with pytest.raises(RuntimeError):
        asyncio.run(llm.complete([{"role": "user", "content": "x"}], _settings(llm_fallback_models="none")))
    assert set(calls) == {"gemini/gemini-flash-latest"}
