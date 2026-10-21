"""Standalone login gate: STANDALONE_ACCESS_CODE."""
import asyncio
import secrets

import pytest
from fastapi import HTTPException

from backend.config import Settings
from backend.app.models import LoginRequest
from backend.main import login

# Generated per run: test fixtures must never look like a real, reusable code.
CODE = "test-" + secrets.token_hex(6)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_login_open_when_no_access_code():
    s = Settings(jwt_secret="x" * 32)
    out = _run(login(LoginRequest(username="demo", password="anything"), s))
    assert out["access_token"]


def test_login_rejects_wrong_access_code():
    s = Settings(jwt_secret="x" * 32, standalone_access_code=CODE)
    with pytest.raises(HTTPException) as exc:
        _run(login(LoginRequest(username="demo", password="wrong"), s))
    assert exc.value.status_code == 401


def test_login_accepts_right_access_code():
    s = Settings(jwt_secret="x" * 32, standalone_access_code=CODE)
    out = _run(login(LoginRequest(username="demo", password=CODE), s))
    assert out["identity"]["user_id"] == "demo"


def test_hosted_without_access_code_refuses_to_start(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    s = Settings(jwt_secret="x" * 32, llm_provider="mock", standalone_access_code="")
    with pytest.raises(SystemExit):
        s.startup_checks()


def test_require_access_code_flag_refuses_to_start(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    s = Settings(jwt_secret="x" * 32, llm_provider="mock", require_access_code=True)
    with pytest.raises(SystemExit):
        s.startup_checks()


def test_hosted_with_access_code_starts(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    s = Settings(jwt_secret="x" * 32, llm_provider="mock", standalone_access_code=CODE)
    s.startup_checks()  # no SystemExit


def test_hosted_login_rejects_empty_and_wrong_codes(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    s = Settings(jwt_secret="x" * 32, standalone_access_code=CODE)
    for pw in ("wrong", CODE.upper(), CODE + " ", "demo"):
        with pytest.raises(HTTPException) as exc:
            _run(login(LoginRequest(username="demo", password=pw), s))
        assert exc.value.status_code == 401


def test_hosted_login_without_code_configured_is_disabled(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    s = Settings(jwt_secret="x" * 32, standalone_access_code="")
    with pytest.raises(HTTPException) as exc:
        _run(login(LoginRequest(username="demo", password="anything"), s))
    assert exc.value.status_code == 503
