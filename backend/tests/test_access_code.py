"""Standalone login gate: STANDALONE_ACCESS_CODE."""
import asyncio

import pytest
from fastapi import HTTPException

from backend.config import Settings
from backend.app.models import LoginRequest
from backend.main import login


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_login_open_when_no_access_code():
    s = Settings(jwt_secret="x" * 32)
    out = _run(login(LoginRequest(username="demo", password="anything"), s))
    assert out["access_token"]


def test_login_rejects_wrong_access_code():
    s = Settings(jwt_secret="x" * 32, standalone_access_code="hisp-2026")
    with pytest.raises(HTTPException) as exc:
        _run(login(LoginRequest(username="demo", password="wrong"), s))
    assert exc.value.status_code == 401


def test_login_accepts_right_access_code():
    s = Settings(jwt_secret="x" * 32, standalone_access_code="hisp-2026")
    out = _run(login(LoginRequest(username="demo", password="hisp-2026"), s))
    assert out["identity"]["user_id"] == "demo"
