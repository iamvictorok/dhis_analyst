"""Expert resolution: ranking, breakdown level, judge fallback, probe, data-used line."""
from __future__ import annotations

import asyncio
import math

import pytest

from backend.app.agent import resolution as R

LEVELS = [
    {"level": 1, "name": "National"},
    {"level": 2, "name": "District"},
    {"level": 3, "name": "Chiefdom"},
    {"level": 4, "name": "Facility"},
]

NAMES = [
    ("u_idsr_mal", "IDSR Malaria", "dataElement"),
    ("u_mal_cpf", "Malaria confirmed Pf (CI)", "dataElement"),
    ("u_mal_cpv", "Malaria confirmed Pv (CI)", "dataElement"),
    ("u_mal_case", "Malaria case - RDT Pf. 0-4 years female", "programIndicator"),
    ("u_inp_mal_d", "Inpatient malaria deaths", "dataElement"),
    ("u_inp_d", "Inpatient deaths", "dataElement"),
    ("u_anc1c", "ANC 1 Coverage", "indicator"),
    ("u_anc4c", "ANC => 4 Coverage", "indicator"),
    ("u_anc4v", "ANC 4th or more visits", "dataElement"),
    ("u_bcgc", "BCG Coverage <1y", "indicator"),
    ("u_measc", "Measles Coverage <1y", "indicator"),
    ("u_p3c", "Penta 3 Coverage <1y", "indicator"),
    ("u_p3d", "Penta3 doses given", "dataElement"),
    ("u_bcgd", "BCG doses given", "dataElement"),
    ("u_bcgpi", "BCG doses", "programIndicator"),
    ("u_fic", "Fully Immunized child", "indicator"),
    ("u_measd", "Measles doses given", "dataElement"),
]


def _catalogue():
    items, df = [], {}
    for uid, name, otype in NAMES:
        toks = set(R.tokens(name))
        items.append({"uid": uid, "label": name, "object_type": otype, "description": "",
                      "tokens": toks, "name_tokens": R.tokens(name)})
        for w in toks:
            df[w] = df.get(w, 0) + 1
    n = len(items)
    idf = {w: math.log((n + 1) / (c + 1)) + 1.0 for w, c in df.items()}
    return {"items": items, "idf": idf, "vocab": set(df), "ou_tokens": {"bo", "kenema", "sierra", "leone"},
            "max_idf": math.log(n + 1) + 1.0}


CAT = _catalogue()


def _top(question: str) -> str:
    q = R.content_tokens(question, {"bo", "kenema", "sierra", "leone"})
    return R.rank_candidates([(q, 1.0)], CAT, limit=3)[0]["label"]


def test_tokens_normalise_abbreviations():
    assert R.tokens("Penta3") == ["penta", "3"]
    assert R.tokens("ANC 4th visits") == ["anc", "4", "visit"]
    assert R.tokens("BCG Coverage <1y") == ["bcg", "coverage", "1", "y"]


@pytest.mark.parametrize("question,expected", [
    ("Penta3 coverage in Kenema last quarter", "Penta 3 Coverage <1y"),
    ("How many ANC4 visits in Bo", "ANC 4th or more visits"),
    ("BCG doses were given by chiefdom in Bo", "BCG doses given"),
    ("Malaria inpatient deaths in Kenema", "Inpatient malaria deaths"),
])
def test_ranking_prefers_precise_names(question, expected):
    assert _top(question) == expected


def test_family_thinning_keeps_differently_named_items():
    q = R.content_tokens("malaria confirmed cases")
    labels = [c["label"] for c in R.rank_candidates([(q, 1.0)], CAT, limit=10, per_family=1)]
    assert "IDSR Malaria" in labels


def test_unknown_tokens_flag_concepts_absent_from_instance():
    q = R.content_tokens("COVID-19 booster vaccine doses in Bo", {"bo"})
    assert set(R.unknown_tokens(q, CAT)) >= {"covid", "booster"}


@pytest.mark.parametrize("message,level", [
    ("Malaria confirmed cases by district in Sierra Leone this year", 2),
    ("Show Penta 3 coverage for each district in 2025", 2),
    ("BCG doses by chiefdom in Bo district", 3),
    ("Which facilities in Bo had the most ANC visits", 4),
    ("Top 5 districts for measles coverage", 2),
    ("ANC visits in Bo district last year", None),
    ("Compare Bo and Kenema", None),
])
def test_parse_breakdown_level(message, level):
    assert R.parse_breakdown_level(message, LEVELS) == level


def _cands(question):
    q = R.content_tokens(question, {"bo", "kenema", "sierra", "leone"})
    return q, R.rank_candidates([(q, 1.0)], CAT, limit=12)


def test_deterministic_judge_vague_question_is_ambiguous():
    q, c = _cands("Show me coverage")
    assert R.deterministic_judge(q, c, R.unknown_tokens(q, CAT))["decision"] == "ambiguous"


def test_deterministic_judge_missing_concept_is_not_available():
    q, c = _cands("Show me COVID-19 booster vaccine doses in Bo last year")
    assert R.deterministic_judge(q, c, R.unknown_tokens(q, CAT))["decision"] == "not_available"


def test_deterministic_judge_specific_question_matches():
    q, c = _cands("Penta3 coverage in Kenema")
    v = R.deterministic_judge(q, c, R.unknown_tokens(q, CAT))
    assert v["decision"] == "match" and c[v["choice"] - 1]["label"] == "Penta 3 Coverage <1y"


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def analytics(self, params):
        self.calls.append(params)
        dx = params["dimension"][0][3:].split(";")
        if "badTextDE01" in dx:
            raise RuntimeError("HTTP 409 E7115 Data elements must be of a value and aggregation type "
                               "that allow aggregation: `[badTextDE01]`")
        return {"rows": [[u, "12"] for u in dx if u.startswith("has")]}


def test_probe_strips_non_aggregatable_items_and_reports_availability():
    client = _FakeClient()
    out = asyncio.run(R.probe_data(client, ["hasData0001", "badTextDE01", "noData00001"], "USER_ORGUNIT", "THIS_YEAR"))
    assert out == {"hasData0001": 12.0, "badTextDE01": None, "noData00001": None}
    assert len(client.calls) == 2


def test_ou_dimension_honours_breakdown_level():
    from backend.app.dhis2.analytics import ou_dimension
    state = {"org_unit": {"label": "Bo", "uid": "O6uvpzGd5pu", "level": 2, "breakdown_level": 3},
             "disaggregations": ["ou_children"]}
    assert ou_dimension(state) == "LEVEL-3;O6uvpzGd5pu"
    state = {"org_unit": {"label": "National", "uid": "UNRESOLVED", "level": 1, "breakdown_level": 2},
             "disaggregations": ["ou_children"]}
    assert ou_dimension(state) == "USER_ORGUNIT;LEVEL-2"
    state = {"org_unit": {"label": "Bo", "uid": "O6uvpzGd5pu", "level": 2}, "disaggregations": []}
    assert ou_dimension(state) == "O6uvpzGd5pu"


def test_normaliser_keeps_category_columns_in_metric_label():
    from backend.app.dhis2.analytics import normalise_analytics_response
    raw = {
        "headers": [{"name": "dx", "meta": True}, {"name": "ou", "meta": True}, {"name": "pe", "meta": True},
                    {"name": "cX5k9anHEHd", "meta": True}, {"name": "value", "meta": False}],
        "rows": [["BCG doses given", "Bo", "2025", "Female", "10"], ["BCG doses given", "Bo", "2025", "Male", "12"]],
        "metaData": {},
    }
    out = normalise_analytics_response(raw, {})
    assert out["rows"] == [["Bo", "2025", "10", "BCG doses given (Female)"], ["Bo", "2025", "12", "BCG doses given (Male)"]]


def test_data_used_line_names_item_uid_level_and_period():
    from backend.app.agent.graph import _data_used_text
    state = {
        "metrics": [{"label": "IDSR Malaria", "uid": "vq2qO3eTrNi", "object_type": "dataElement"}],
        "org_unit": {"label": "Sierra Leone", "uid": "ImspTQPwCqd", "level": 1, "level_name": "National",
                     "breakdown_level": 2, "breakdown_level_name": "District"},
        "periods": ["THIS_YEAR"],
        "disaggregations": ["ou_children"],
        "dhis2_data": {"rows": [["Bo", "2026", "1", "IDSR Malaria"]], "metadata": {}},
        "resolution_notes": ["note one"],
    }
    line = _data_used_text(state)
    assert "IDSR Malaria" in line and "vq2qO3eTrNi" in line
    assert "District (LEVEL-2)" in line and "THIS_YEAR" in line and "2026" in line
    assert "note one" in line


def test_fallback_candidates_used_when_first_choice_is_empty(monkeypatch):
    from backend.app.agent import graph

    async def fake_fetch(state, settings=None):
        uid = state["metrics"][0]["uid"]
        state["dhis2_data"] = {"rows": [["Bo", "2026", "5", "x"]] if uid == "goodUid0001" else []}
        return state

    monkeypatch.setattr(graph, "fetch_dhis2", fake_fetch)
    state = {"session_id": "s", "metrics": [{
        "label": "Empty one", "uid": "emptyUid001", "object_type": "dataElement",
        "fallbacks": [{"label": "Also empty", "uid": "emptyUid002", "object_type": "dataElement"},
                      {"label": "Good one", "uid": "goodUid0001", "object_type": "dataElement"}],
    }], "dhis2_data": {"rows": []}}
    out = asyncio.run(graph._try_fallback_candidates(state, None))
    assert out["metrics"][0]["uid"] == "goodUid0001"
    assert any("next-best" in n for n in out["resolution_notes"])


def test_not_available_message_streams_without_data(monkeypatch, tmp_path):
    from backend.app.agent import graph
    from backend.app.models import ChatRequest
    from backend.config import Settings

    async def fake_resolve(state, settings):
        state["not_available_message"] = "**COVID-19 booster doses** isn't available in this DHIS2 instance."
        return state

    async def boom(*a, **k):  # must never be reached
        raise AssertionError("fetch must not run for an unavailable concept")

    monkeypatch.setattr(graph, "resolve_metadata", fake_resolve)
    monkeypatch.setattr(graph, "fetch_dhis2", boom)
    s = Settings(llm_provider="mock", database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")

    async def collect():
        return [c async for c in graph.run_agent_stream(ChatRequest(message="COVID-19 booster doses in Bo"), s)]

    body = "".join(asyncio.run(collect()))
    assert "isn't available" in body and "chart_config" not in body
