"""Map feature: intent rules, geometry parsing/join, fallbacks, no spurious map note.

Fixtures use a made-up instance ("Freedonia") with its own level names
(Country / Region / Province / Site) so nothing here depends on the Sierra
Leone demo database.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.agent import map_builder as MB
from backend.app.agent.intent import classify_intent, decide_map_mode, wants_map

ROOT = "FrdRoot0001"
REG_A, REG_B, REG_C, REG_D = "RegionAAA01", "RegionBBB02", "RegionCCC03", "RegionDDD04"
SITE_1 = "SiteXYZ0001"

# geoFeatures-shaped items (as /api/geoFeatures returns them; "co" is a JSON string)
MULTI = [[[[10.0, 50.0], [10.5, 50.0], [10.5, 50.5], [10.0, 50.5], [10.0, 50.0]]],
         [[[11.0, 51.0], [11.2, 51.0], [11.2, 51.2], [11.0, 51.2], [11.0, 51.0]]]]
POLY = [[[12.0, 49.0], [12.8, 49.0], [12.8, 49.6], [12.0, 49.6], [12.0, 49.0]]]
GEOFEATURES = [
    {"id": REG_A, "na": "North Marches", "ty": 3, "co": json.dumps(MULTI), "le": 2, "pg": ROOT, "pi": ROOT},
    {"id": REG_B, "na": "South Vale", "ty": 2, "co": json.dumps(POLY), "le": 2, "pg": ROOT, "pi": ROOT},
    {"id": REG_C, "na": "East Fen", "ty": 2, "co": json.dumps([[[13.0, 50.0], [13.4, 50.0], [13.4, 50.3], [13.0, 50.0]]]),
     "le": 2, "pg": ROOT, "pi": ROOT},
]
LEVELS = [{"level": 1, "name": "Country"}, {"level": 2, "name": "Region"},
          {"level": 3, "name": "Province"}, {"level": 4, "name": "Site"}]


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Map of ANC 1 coverage by district, Sierra Leone, 2025",
    "Show Penta 3 coverage by district on a map, last 12 months",
    "Where is malaria incidence highest this year?",
    "Show the geographic distribution of measles cases",
    "choropleth of OPV3 coverage by province",
])
def test_map_cues_are_detected(text):
    assert wants_map(text)


@pytest.mark.parametrize("text", [
    "What was BCG coverage under 1 year by district in Sierra Leone in 2025?",
    "Malaria confirmed cases by district in Sierra Leone this year",
    "Show me the roadmap for the HMIS rollout",
    "Where can I find the ANC indicators?",
    "Compare Penta 3 coverage across regions in 2025",
])
def test_breakdowns_without_map_cue_are_not_maps(text):
    assert not wants_map(text)


def test_llm_map_guess_without_cue_becomes_dashboard():
    # The n1 bug: the model labelled a plain "by district" question as a map.
    q = "What was BCG coverage under 1 year by district in Sierra Leone in 2025?"
    assert decide_map_mode(q, "map") == "dashboard"


def test_explicit_map_beats_dashboard_but_not_report_or_export():
    assert decide_map_mode("Show ANC 1 by region on a map", "dashboard") == "map"
    assert decide_map_mode("Write a report on ANC 1 with a map", "report") == "report"
    assert decide_map_mode("Export the ANC 1 map data to Excel", "export") == "export"
    assert decide_map_mode("anything", "conversational", forced_mode="map") == "map"
    assert decide_map_mode("Map of ANC 1", "map", forced_mode="dashboard") == "dashboard"


@pytest.mark.parametrize("q,mode", [
    ("Show me ANC 1 Coverage in Sierra Leone for the last 12 months", "dashboard"),
    ("Show Penta 3 coverage <1y for each district in Sierra Leone for 2025", "dashboard"),
    ("Show me the trend of IDSR Malaria in Sierra Leone for the last 12 months", "dashboard"),
    ("Write a programme review report on ANC 1 Coverage in Sierra Leone for the last 12 months", "report"),
    ("Export ANC 1 Coverage by district for the last 12 months to Excel", "export"),
])
def test_demo_chips_never_become_maps(q, mode):
    assert classify_intent(q, forced_mode=mode)["output_mode"] == mode
    assert decide_map_mode(q, "map") != "map"


def test_keyword_classifier_routes_map_requests_to_map():
    r = classify_intent("Map of TB cases by region")
    assert r["output_mode"] == "map" and r["viz_types"] == ["map"]


# ---------------------------------------------------------------------------
# Geometry parsing (all shapes, all API generations)
# ---------------------------------------------------------------------------

def test_geofeature_shapes_point_polygon_multipolygon():
    assert MB.geo_feature_to_geometry({"ty": 1, "co": "[7.5,9.1]"}) == {"type": "Point", "coordinates": [7.5, 9.1]}
    assert MB.geo_feature_to_geometry({"ty": 2, "co": json.dumps(POLY)})["type"] == "Polygon"
    assert MB.geo_feature_to_geometry({"ty": 3, "co": json.dumps(MULTI)})["type"] == "MultiPolygon"
    # Some instances tag a multipolygon as ty=2: detect it by nesting depth.
    assert MB.geo_feature_to_geometry({"ty": 2, "co": json.dumps(MULTI)})["type"] == "MultiPolygon"
    assert MB.geo_feature_to_geometry({"ty": 2, "co": "not json"}) is None


def test_org_unit_geometry_field_and_legacy_coordinates():
    assert MB.org_unit_geometry_to_geojson({"type": "MultiPolygon", "coordinates": MULTI})["type"] == "MultiPolygon"
    assert MB.org_unit_geometry_to_geojson({"type": "LineString", "coordinates": [[1, 2]]}) is None
    legacy_point = MB.legacy_coordinates_to_geometry("[3.4,6.5]", "POINT")
    assert legacy_point == {"type": "Point", "coordinates": [3.4, 6.5]}
    assert MB.legacy_coordinates_to_geometry(json.dumps(MULTI), "MULTI_POLYGON")["type"] == "MultiPolygon"
    assert MB.legacy_coordinates_to_geometry(json.dumps(POLY), "POLYGON")["type"] == "Polygon"
    assert MB.legacy_coordinates_to_geometry(json.dumps(POLY), None)["type"] == "Polygon"
    assert MB.legacy_coordinates_to_geometry("", "POINT") is None


def test_simplify_keeps_rings_closed_and_valid():
    ring = [[10 + i * 0.0001, 50.0] for i in range(200)] + [[10.02, 50.5], [10.0, 50.5], [10.0, 50.0]]
    g = MB.simplify_geometry({"type": "Polygon", "coordinates": [ring]})
    out = g["coordinates"][0]
    assert out[0] == out[-1] and len(out) >= 4 and len(out) < len(ring)


def test_parse_ou_dimension():
    assert MB.parse_ou_dimension(f"LEVEL-3;{ROOT}") == ([ROOT], 3, False)
    assert MB.parse_ou_dimension("USER_ORGUNIT;LEVEL-2") == ([], 2, True)


# ---------------------------------------------------------------------------
# Join + legend
# ---------------------------------------------------------------------------

def _geometry():
    out = {}
    for f in GEOFEATURES:
        out[f["id"]] = {"name": f["na"], "geometry": MB.geo_feature_to_geometry(f), "root": ROOT}
    out[SITE_1] = {"name": "Harbour Clinic", "geometry": {"type": "Point", "coordinates": [12.4, 49.3]}, "root": ROOT}
    return out


def test_join_by_uid_draws_what_has_geometry_and_lists_the_rest():
    values = {REG_A: 82.5, REG_B: 40.0, SITE_1: 61.0, REG_D: 70.0}  # REG_D has no geometry
    names = {REG_A: "North Marches", REG_B: "South Vale", SITE_1: "Harbour Clinic", REG_D: "West Downs"}
    j = MB.join_values_to_geometry(values, names, _geometry(), "%")
    by_id = {f["id"]: f for f in j["geojson"]["features"]}
    assert by_id[REG_A]["geometry"]["type"] == "MultiPolygon"
    assert by_id[REG_A]["properties"]["value"] == 82.5 and by_id[REG_A]["properties"]["label"] == "82.5%"
    assert by_id[REG_C]["properties"]["value"] is None and by_id[REG_C]["properties"]["color"] == MB.NO_DATA_COLOR
    assert [p["id"] for p in j["points"]] == [SITE_1]  # facilities become circle markers
    assert j["no_geometry"] == ["West Downs"]
    assert j["no_value"] == ["East Fen"]
    assert j["drawn_with_value"] == 3
    # bbox fitted to the returned features, not a fixed country box
    assert j["bbox"] == [10.0, 49.0, 13.4, 51.2]
    # every drawn value sits in a legend class
    for f in j["geojson"]["features"]:
        v = f["properties"]["value"]
        if v is not None:
            c = j["legend"][f["properties"]["class"]]
            assert c["from"] <= v <= c["to"]


def test_legend_classes_cover_range_and_handle_single_value():
    cls = MB.legend_classes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert cls[0]["from"] == 1 and cls[-1]["to"] == 10 and 2 <= len(cls) <= 5
    assert all(a["to"] == b["from"] for a, b in zip(cls, cls[1:]))
    one = MB.legend_classes([42.0, 42.0])
    assert len(one) == 1 and one[0]["from"] == one[0]["to"] == 42.0
    assert MB.legend_classes([]) == []


# ---------------------------------------------------------------------------
# build_map against a fake (non-Sierra-Leone) instance
# ---------------------------------------------------------------------------

class FakeClient:
    """Stands in for DHIS2Client; records calls; never touches auth."""

    calls: list = []
    geofeatures: list | Exception = GEOFEATURES
    org_units: list = []

    def __init__(self, settings, token=None):
        self.token = token

    async def analytics(self, params):
        FakeClient.calls.append(("analytics", params))
        return {
            "headers": [{"name": "dx"}, {"name": "ou"}, {"name": "value"}],
            "rows": [["IndFrd00001", REG_A, "82.5"], ["IndFrd00001", REG_B, "40.0"], ["IndFrd00001", REG_D, "70"]],
            "metaData": {
                "items": {REG_A: {"name": "North Marches"}, REG_B: {"name": "South Vale"},
                          REG_D: {"name": "West Downs"}, ROOT: {"name": "Freedonia"},
                          "2025": {"name": "2025"}, "IndFrd00001": {"name": "Clinic visit coverage"}},
                "dimensions": {"pe": ["2025"], "ou": [REG_A, REG_B, REG_D]},
            },
        }

    async def get(self, path, params=None):
        FakeClient.calls.append((path, params))
        if "geoFeatures" in path:
            if isinstance(FakeClient.geofeatures, Exception):
                raise FakeClient.geofeatures
            return {"geoFeatures": FakeClient.geofeatures}
        if path.startswith("/api/indicators/"):
            return {"indicatorType": {"factor": 100}}
        if path == "/api/me.json":
            return {"organisationUnits": [{"id": ROOT}]}
        if path == "/api/organisationUnits.json":
            return {"organisationUnits": FakeClient.org_units}
        raise AssertionError(path)


def _settings():
    from backend.config import Settings
    return Settings(llm_provider="mock", dhis2_base_url="https://freedonia.example/dhis",
                    dhis2_base_urls="https://freedonia.example/dhis",
                    dhis2_service_account_user="svc", dhis2_service_account_pass="x")


def _map_state(org=None):
    return {
        "metrics": [{"label": "Clinic visit coverage", "uid": "IndFrd00001", "object_type": "indicator"}],
        "periods": ["2025"],
        "disaggregations": ["ou_children"],
        "org_unit": org or {"label": "National", "uid": "UNRESOLVED", "level": 1,
                            "breakdown_level": 2, "breakdown_level_name": "Region"},
        "_dhis2_token": None,
    }


@pytest.fixture
def fake_client(monkeypatch):
    import backend.app.dhis2.client as client_mod
    MB._GEOMETRY_CACHE.clear()
    MB._UNIT_CACHE.clear()
    FakeClient.calls = []
    FakeClient.geofeatures = GEOFEATURES
    FakeClient.org_units = []
    monkeypatch.setattr(client_mod, "DHIS2Client", FakeClient)
    return FakeClient


def test_build_map_on_another_instance(fake_client):
    p = asyncio.run(MB.build_map(_map_state(), _settings()))
    assert p["title"] == "Clinic visit coverage by region, Freedonia, 2025"
    assert p["unit"] == "%"
    assert {f["id"] for f in p["geojson"]["features"]} == {REG_A, REG_B, REG_C}
    assert any("West Downs" in n for n in p["notes"])  # data but no geometry -> listed
    assert p["table"]["rows"][0] == ["North Marches", 82.5]
    assert p["analytics_params"]["filter"] == ["pe:2025"]
    assert "ou:USER_ORGUNIT;LEVEL-2" in p["analytics_params"]["dimension"]
    # one geometry request, then cached per instance + ou dimension
    asyncio.run(MB.build_map(_map_state(), _settings()))
    assert sum(1 for c in fake_client.calls if "geoFeatures" in c[0]) == 1


def test_build_map_falls_back_to_org_unit_api_with_legacy_coordinates(fake_client):
    fake_client.geofeatures = RuntimeError("404 geoFeatures")
    fake_client.org_units = [
        {"id": REG_A, "displayName": "North Marches", "path": f"/{ROOT}/{REG_A}", "featureType": "MULTI_POLYGON",
         "coordinates": json.dumps(MULTI)},
        {"id": REG_B, "displayName": "South Vale", "path": f"/{ROOT}/{REG_B}",
         "geometry": {"type": "Polygon", "coordinates": POLY}},
    ]
    p = asyncio.run(MB.build_map(_map_state(), _settings()))
    types = sorted(f["geometry"]["type"] for f in p["geojson"]["features"])
    assert types == ["MultiPolygon", "Polygon"]
    assert p["title"].endswith("Freedonia, 2025")
    org_call = [c for c in fake_client.calls if c[0] == "/api/organisationUnits.json"][0][1]
    assert f"path:like:{ROOT}" in org_call["filter"] and "level:eq:2" in org_call["filter"]


def test_build_map_without_any_geometry_is_unavailable(fake_client):
    fake_client.geofeatures = []
    with pytest.raises(MB.MapUnavailable) as exc:
        asyncio.run(MB.build_map(_map_state(), _settings()))
    assert "no boundaries or coordinates" in str(exc.value)


def test_build_map_needs_a_breakdown_and_credentials(fake_client):
    st = _map_state({"label": "North Marches", "uid": REG_A, "level": 2})
    st["disaggregations"] = []
    with pytest.raises(MB.MapUnavailable):
        asyncio.run(MB.build_map(st, _settings()))
    from backend.config import Settings
    with pytest.raises(MB.MapUnavailable):
        asyncio.run(MB.build_map(_map_state(), Settings(llm_provider="mock", dhis2_service_account_user="")))


def test_map_mode_defaults_to_one_level_down_with_instance_level_names(monkeypatch):
    from backend.app.agent import resolution
    from backend.app.agent.nodes import metadata_resolve as M

    async def fake_levels(settings, token=None):
        return LEVELS

    monkeypatch.setattr(resolution, "org_levels", fake_levels)
    st = {"messages": [{"role": "user", "content": "Map of clinic visit coverage in North Marches"}],
          "org_unit": {"label": "North Marches", "uid": REG_A, "level": 2},
          "disaggregations": [], "output_mode": "map"}
    asyncio.run(M._apply_breakdown_level(st, None))
    assert st["org_unit"]["breakdown_level"] == 3
    assert st["org_unit"]["breakdown_level_name"] == "Province"
    assert "ou_children" in st["disaggregations"]


# ---------------------------------------------------------------------------
# Graph: map event, honest fallback, no spurious note
# ---------------------------------------------------------------------------

def _graph_settings():
    from backend.config import Settings
    return Settings(llm_provider="mock", dhis2_service_account_user="svc", dhis2_service_account_pass="x",
                    dhis2_base_url="https://freedonia.example/dhis")


async def _fake_resolve(state, settings=None):
    state["metrics"] = [{"label": "Clinic visit coverage", "uid": "IndFrd00001", "object_type": "indicator",
                         "uid_confidence": 1.0}]
    state["org_unit"] = {"label": "National", "uid": "UNRESOLVED", "level": 1,
                         "breakdown_level": 2, "breakdown_level_name": "Region"}
    state["disaggregations"] = ["ou_children"]
    state["periods"] = ["2025"]
    state["clarification_needed"] = False
    return state


async def _fake_fetch(state, settings=None):
    state["dhis2_data"] = {
        "rows": [["North Marches", "2025", "82.5", "Clinic visit coverage"],
                 ["South Vale", "2025", "40.0", "Clinic visit coverage"]],
        "headers": ["Organisation unit", "Period", "Value", "Metric"],
        "metadata": {"data_source": "analytics_api"},
    }
    return state


def _run(monkeypatch, message, build=None):
    from backend.app.agent import graph
    from backend.app.models import ChatRequest

    monkeypatch.setattr(graph, "resolve_metadata", _fake_resolve)
    monkeypatch.setattr(graph, "fetch_dhis2", _fake_fetch)
    if build is not None:
        monkeypatch.setattr(graph, "build_map", build)

    async def collect():
        return "".join([c async for c in graph.run_agent_stream(ChatRequest(message=message), _graph_settings())])

    return asyncio.run(collect())


def test_graph_emits_map_config_for_map_request(monkeypatch, fake_client):
    body = _run(monkeypatch, "Map of clinic visit coverage by region in 2025")
    assert "event: map_config" in body
    assert "coming soon" not in body and "couldn't draw a map" not in body
    assert "**Highest:** North Marches (82.5%)" in body
    assert "**Data used:**" in body


def test_graph_map_failure_falls_back_to_chart_with_honest_note(monkeypatch):
    async def no_geometry(state, settings):
        raise MB.MapUnavailable("DHIS2 has no boundaries or coordinates for the region units in this answer")

    body = _run(monkeypatch, "Map of clinic visit coverage by region in 2025", no_geometry)
    assert "I couldn't draw a map: DHIS2 has no boundaries" in body
    assert "event: chart_config" in body and "event: map_config" not in body


def test_no_spurious_map_note_on_plain_breakdown(monkeypatch):
    called = []

    async def should_not_run(state, settings):
        called.append(1)
        raise AssertionError("map built for a non-map question")

    body = _run(monkeypatch, "What was clinic visit coverage by region in 2025?", should_not_run)
    assert not called
    assert "coming soon" not in body and "couldn't draw a map" not in body
    assert "event: map_config" not in body
