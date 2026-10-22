"""Live -> fallback -> cached-snapshot failover, snapshot store, labels."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.config import Settings
from backend.app.dhis2 import failover
from backend.app.dhis2.client import DHIS2Client

PRIMARY = "https://play.example/stable-2-41-10"
SECONDARY = "https://play.example/stable-2-40-12"

ANALYTICS_PARAMS = {
    "dimension": ["dx:Uvn6LCg7dVU", "ou:LEVEL-2;ImspTQPwCqd", "pe:LAST_12_MONTHS"],
    "displayProperty": "NAME",
    "outputIdScheme": "NAME",
    "skipMeta": "false",
}
ANALYTICS_PAYLOAD = {
    "headers": [{"name": "dx"}, {"name": "ou"}, {"name": "pe"}, {"name": "value"}],
    "rows": [["ANC 1 Coverage", "Bo", "September 2026", "91.2"]],
    "metaData": {},
}


@pytest.fixture(autouse=True)
def _reset():
    failover.reset_state()
    yield
    failover.reset_state()


def _write_snapshot(tmp_path: Path, entries: dict | None = None, date: str = "2026-10-21") -> Path:
    path = tmp_path / "snap.json"
    if entries is None:
        entries = {failover.canonical_key("/api/analytics.json", ANALYTICS_PARAMS): ANALYTICS_PAYLOAD}
    path.write_text(json.dumps({"meta": {"recorded_at": date}, "entries": entries}))
    return path


def _settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        llm_provider="mock",
        dhis2_base_url=PRIMARY,
        dhis2_base_urls=f"{PRIMARY},{SECONDARY}",
        dhis2_service_account_user="admin",
        dhis2_service_account_pass="district",
        dhis2_snapshot_path=str(_write_snapshot(tmp_path)),
    )
    base.update(kw)
    return Settings(**base)


def _patch_probe(monkeypatch, up: set[str]):
    async def fake_probe_one(settings, url, timeout):
        return {"url": url, "ok": url in up, "version": "2.41" if url in up else None, "ms": 1}

    monkeypatch.setattr(failover, "_probe_one", fake_probe_one)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- keys + store ----------------------------------------------------------

def test_canonical_key_ignores_param_and_item_order():
    a = failover.canonical_key("/api/analytics.json", ANALYTICS_PARAMS)
    shuffled = {
        "skipMeta": "false",
        "outputIdScheme": "NAME",
        "displayProperty": "NAME",
        "dimension": ["pe:LAST_12_MONTHS", "ou:ImspTQPwCqd;LEVEL-2", "dx:Uvn6LCg7dVU"],
    }
    assert a == failover.canonical_key("api/analytics.json", shuffled)


def test_store_exact_relaxed_and_miss(tmp_path):
    store = failover.SnapshotStore(_write_snapshot(tmp_path))
    assert store.lookup("/api/analytics.json", ANALYTICS_PARAMS) == ANALYTICS_PAYLOAD
    # Same dimensions, different display options -> relaxed match.
    relaxed = {"dimension": ANALYTICS_PARAMS["dimension"], "skipMeta": "true"}
    assert store.lookup("/api/analytics.json", relaxed) == ANALYTICS_PAYLOAD
    with pytest.raises(failover.SnapshotMiss):
        store.lookup("/api/analytics.json", {"dimension": ["dx:other", "pe:2025"]})
    with pytest.raises(failover.SnapshotMiss):
        store.lookup("/api/dataSets.json", {"paging": "false"})


def test_recorder_round_trip(tmp_path):
    out = tmp_path / "rec.json"
    rec = failover.SnapshotRecorder(out)
    rec.record(PRIMARY, "/api/analytics.json", ANALYTICS_PARAMS, ANALYTICS_PAYLOAD)
    store = failover.SnapshotStore(out)
    assert store.lookup("/api/analytics.json", ANALYTICS_PARAMS) == ANALYTICS_PAYLOAD
    assert store.meta["source_base_url"] == PRIMARY
    assert store.recorded_at


def test_base_urls_falls_back_to_single_url():
    s = Settings(llm_provider="mock", dhis2_base_url=PRIMARY + "/", dhis2_base_urls="")
    assert failover.base_urls(s) == [PRIMARY]
    s2 = Settings(llm_provider="mock", dhis2_base_urls=f" {PRIMARY} ,{SECONDARY},{PRIMARY}")
    assert failover.base_urls(s2) == [PRIMARY, SECONDARY]


# --- probe / mode selection --------------------------------------------------

def test_probe_primary_up_is_live(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _patch_probe(monkeypatch, {PRIMARY, SECONDARY})
    st = _run(failover.probe(s))
    assert st["mode"] == "live" and st["base_url"] == PRIMARY


def test_probe_primary_down_uses_fallback(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _patch_probe(monkeypatch, {SECONDARY})
    st = _run(failover.probe(s))
    assert st["mode"] == "fallback" and st["base_url"] == SECONDARY
    assert failover.current_target(s) == ("fallback", SECONDARY)


def test_probe_all_down_serves_snapshot(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _patch_probe(monkeypatch, set())
    st = _run(failover.probe(s))
    assert st["mode"] == "cached"
    assert st["label"] == "Cached data as of 21 Oct 2026"
    assert st["base_url"] is None


def test_probe_all_down_snapshot_off_is_down(tmp_path, monkeypatch):
    s = _settings(tmp_path, dhis2_snapshot_mode="off")
    _patch_probe(monkeypatch, set())
    assert _run(failover.probe(s))["mode"] == "down"


def test_probe_all_down_without_snapshot_file_is_down(tmp_path, monkeypatch):
    s = _settings(tmp_path, dhis2_snapshot_path=str(tmp_path / "missing.json"))
    _patch_probe(monkeypatch, set())
    assert _run(failover.probe(s))["mode"] == "down"


def test_snapshot_always_forces_cached(tmp_path, monkeypatch):
    s = _settings(tmp_path, dhis2_snapshot_mode="always")
    _patch_probe(monkeypatch, {PRIMARY})
    assert _run(failover.probe(s))["mode"] == "cached"
    assert failover.current_target(s)[0] == "cached"


def test_status_never_contains_credentials(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _patch_probe(monkeypatch, {PRIMARY})
    st = _run(failover.probe(s))
    assert "district" not in json.dumps(st)


# --- client integration ------------------------------------------------------

def test_client_serves_snapshot_when_all_instances_down(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _patch_probe(monkeypatch, set())
    client = DHIS2Client(s)
    calls = []

    async def dead_http_get(base_url, path, params=None):
        calls.append(base_url)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(client, "_http_get", dead_http_get)

    async def go():
        tracker = failover.begin_request()
        out = await client.analytics(ANALYTICS_PARAMS)
        return out, tracker

    out, tracker = _run(go())
    assert out == ANALYTICS_PAYLOAD
    assert calls == [PRIMARY]          # one failed live try, then snapshot
    assert tracker["mode"] == "cached"


def test_client_moves_to_fallback_instance(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _patch_probe(monkeypatch, {SECONDARY})
    client = DHIS2Client(s)
    calls = []

    async def http_get(base_url, path, params=None):
        calls.append(base_url)
        if base_url == PRIMARY:
            raise httpx.ConnectError("down")
        return {"rows": [["x"]]}

    monkeypatch.setattr(client, "_http_get", http_get)

    async def go():
        tracker = failover.begin_request()
        out = await client.analytics(ANALYTICS_PARAMS)
        return out, tracker

    out, tracker = _run(go())
    assert out == {"rows": [["x"]]}
    assert calls == [PRIMARY, SECONDARY]
    assert tracker == {"mode": "fallback", "base_url": SECONDARY}


def test_client_bad_request_does_not_fail_over(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    probed = []

    async def fake_probe(settings, force=True):
        probed.append(1)

    monkeypatch.setattr(failover, "probe", fake_probe)
    client = DHIS2Client(s)
    req = httpx.Request("GET", PRIMARY + "/api/analytics.json")
    resp = httpx.Response(409, request=req, json={"message": "bad dimension"})

    async def http_get(base_url, path, params=None):
        raise httpx.HTTPStatusError("409", request=req, response=resp)

    monkeypatch.setattr(client, "_http_get", http_get)
    with pytest.raises(httpx.HTTPStatusError):
        _run(client.analytics(ANALYTICS_PARAMS))
    assert probed == []


def test_client_snapshot_miss_is_clear(tmp_path, monkeypatch):
    s = _settings(tmp_path, dhis2_snapshot_mode="always")
    client = DHIS2Client(s)
    with pytest.raises(failover.SnapshotMiss) as exc:
        _run(client.analytics({"dimension": ["dx:zzz", "pe:2020"]}))
    assert "offline demo snapshot" in str(exc.value)


def test_token_requests_bypass_failover(tmp_path, monkeypatch):
    s = _settings(tmp_path, dhis2_snapshot_mode="always")
    client = DHIS2Client(s, token="pat")
    seen = []

    async def http_get(base_url, path, params=None):
        seen.append(base_url)
        return {"ok": True}

    monkeypatch.setattr(client, "_http_get", http_get)
    assert _run(client.get("/api/me.json")) == {"ok": True}
    assert seen == [PRIMARY]


def test_recording_captures_live_responses(tmp_path, monkeypatch):
    out = tmp_path / "rec.json"
    s = _settings(tmp_path, dhis2_snapshot_mode="off", dhis2_snapshot_record_path=str(out))
    client = DHIS2Client(s)

    async def http_get(base_url, path, params=None):
        return ANALYTICS_PAYLOAD

    monkeypatch.setattr(client, "_http_get", http_get)
    _run(client.analytics(ANALYTICS_PARAMS))
    assert failover.SnapshotStore(out).lookup("/api/analytics.json", ANALYTICS_PARAMS) == ANALYTICS_PAYLOAD


# --- agent labelling ---------------------------------------------------------

def _collect(gen):
    async def go():
        return [chunk async for chunk in gen]

    return _run(go())


async def _fake_resolve(state, settings=None):
    state["metrics"] = [{"label": "ANC 1 Coverage", "uid": "Uvn6LCg7dVU", "object_type": "indicator", "uid_confidence": 1.0}]
    state["org_unit"] = {"label": "Sierra Leone", "uid": "ImspTQPwCqd", "level": 1}
    state["periods"] = ["LAST_12_MONTHS"]
    state["clarification_needed"] = False
    return state


def _fake_fetch(mode):
    async def fetch(state, settings=None):
        failover.note_source(mode, None if mode == "cached" else SECONDARY)
        state["dhis2_data"] = {
            "rows": [["Sierra Leone", "September 2026", "91.2", "ANC 1 Coverage"]],
            "headers": ["Organisation unit", "Period", "Value", "Metric"],
            "metadata": {"data_source": "analytics_api"},
        }
        return state

    return fetch


@pytest.mark.parametrize("output_mode", ["conversational", "report", "dashboard", "export"])
def test_cached_answers_are_labelled(tmp_path, monkeypatch, output_mode):
    from backend.app.agent import graph
    from backend.app.models import ChatRequest

    s = _settings(tmp_path)
    monkeypatch.setattr(graph, "resolve_metadata", _fake_resolve)
    monkeypatch.setattr(graph, "fetch_dhis2", _fake_fetch("cached"))
    chunks = _collect(graph.run_agent_stream(
        ChatRequest(message="Show me ANC 1 Coverage in Sierra Leone for the last 12 months",
                    output_mode=output_mode),
        s,
    ))
    body = "".join(chunks)
    assert "event: data_source" in body
    assert '"mode": "cached"' in body
    assert "Cached data as of 21 Oct 2026" in body
    if output_mode == "export":
        assert "demo_snapshot" in body


def test_live_answers_have_no_cached_label(tmp_path, monkeypatch):
    from backend.app.agent import graph
    from backend.app.models import ChatRequest

    s = _settings(tmp_path)
    monkeypatch.setattr(graph, "resolve_metadata", _fake_resolve)
    monkeypatch.setattr(graph, "fetch_dhis2", _fake_fetch("live"))
    body = "".join(_collect(graph.run_agent_stream(
        ChatRequest(message="Show me ANC 1 Coverage in Sierra Leone for the last 12 months"), s,
    )))
    assert "Cached data as of" not in body


def test_map_that_cannot_be_drawn_falls_back_to_chart_with_honest_note(tmp_path, monkeypatch):
    from backend.app.agent import graph
    from backend.app.models import ChatRequest

    s = _settings(tmp_path)
    real_classify = graph.classify_intent_llm

    async def classify_as_map(*a, **kw):
        intent = await real_classify(*a, **kw)
        intent["output_mode"] = "map"
        intent["viz_types"] = ["map"]
        return intent

    monkeypatch.setattr(graph, "classify_intent_llm", classify_as_map)
    monkeypatch.setattr(graph, "resolve_metadata", _fake_resolve)
    monkeypatch.setattr(graph, "fetch_dhis2", _fake_fetch("live"))
    body = "".join(_collect(graph.run_agent_stream(
        ChatRequest(message="Show me a map of ANC 1 Coverage by district in Sierra Leone for 2025"), s,
    )))
    # The fake resolver gives no sub-unit breakdown, so there is nothing to map:
    # the user asked for a map, so they get a chart plus the real reason.
    assert "I couldn't draw a map" in body
    assert "coming soon" not in body
    assert "event: chart_config" in body
    assert "event: map_config" not in body
    assert 'id=\\"map\\"' not in body


# --- committed snapshot + demo questions -------------------------------------

REPO_SNAPSHOT = Path(__file__).resolve().parents[1] / "demo_snapshot" / "dhis2_snapshot.json"
REPO_QUESTIONS = Path(__file__).resolve().parents[1] / "demo_snapshot" / "demo_questions.json"


def test_demo_questions_file_has_the_five_verified_questions():
    qs = json.loads(REPO_QUESTIONS.read_text())
    assert [q["id"] for q in qs] == ["anc1", "penta3", "idsr", "report", "export"]
    assert all(q["question"] and q["output_mode"] for q in qs)


def test_committed_snapshot_covers_metadata_and_analytics():
    store = failover.SnapshotStore(REPO_SNAPSHOT).load()
    assert store.recorded_at
    keys = list(store.entries)
    for endpoint in ("dataElements", "indicators", "organisationUnits", "programIndicators", "categories"):
        assert any(k.startswith(f"/api/{endpoint}.json") for k in keys), endpoint
    assert sum(k.startswith("/api/analytics") for k in keys) >= 5


def test_user_orgunit_alias_matches_country_uid(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({
        "meta": {"recorded_at": "2026-10-21", "ou_aliases": {"USER_ORGUNIT": "ImspTQPwCqd"}},
        "entries": {failover.canonical_key("/api/analytics.json", ANALYTICS_PARAMS): ANALYTICS_PAYLOAD},
    }))
    store = failover.SnapshotStore(path)
    asked = {"dimension": ["dx:Uvn6LCg7dVU", "ou:USER_ORGUNIT;LEVEL-2", "pe:LAST_12_MONTHS"]}
    assert store.lookup("/api/analytics.json", asked) == ANALYTICS_PAYLOAD
