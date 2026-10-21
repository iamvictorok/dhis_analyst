"""Generic rules: granularity periods, reporting timeliness wording, tracker routing,
count-vs-rate preference."""
from __future__ import annotations

from datetime import date

import pytest

from backend.app.agent import resolution as R
from backend.app.agent.intent import _merge_llm_intent, classify_intent, detect_granularity, expand_periods
from backend.app.agent.nodes.fetch_dhis2 import effective_strategy
from backend.app.agent.nodes.metadata_resolve import is_reporting_question

TODAY = date(2026, 10, 21)
MONTHS_2025 = [f"2025{m:02d}" for m in range(1, 13)]


# --- 1. granularity -------------------------------------------------------

@pytest.mark.parametrize("text,gran", [
    ("Monthly trend of inpatient malaria cases in 2025", "monthly"),
    ("malaria cases by month in 2025", "monthly"),
    ("weekly IDSR malaria for 2025", "weekly"),
    ("ANC 1 coverage each quarter of 2024", "quarterly"),
    ("ANC 1 coverage for the last 4 quarters", None),
    ("ANC 1 coverage in 2025", None),
])
def test_detect_granularity(text, gran):
    assert detect_granularity(text) == gran


def test_monthly_fixed_year_expands_to_twelve_months():
    assert expand_periods("Monthly Penta 3 coverage for Bo in 2025", ["2025"], TODAY) == MONTHS_2025


def test_quarterly_range_and_current_year_is_capped():
    out = expand_periods("quarterly ANC 1 coverage 2025 to 2026", ["2025", "2026"], TODAY)
    assert out == ["2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3", "2026Q4"]
    out = expand_periods("monthly ANC 1 coverage in 2026", ["2026"], TODAY)
    assert out[0] == "202601" and out[-1] == "202610" and len(out) == 10


def test_weekly_year_uses_iso_week_count():
    out = expand_periods("weekly malaria in 2020", ["2020"], TODAY)
    assert out[0] == "2020W1" and out[-1] == "2020W53"


def test_relative_years_with_granularity():
    assert expand_periods("monthly malaria this year", ["THIS_YEAR"], TODAY) == ["MONTHS_THIS_YEAR"]
    assert expand_periods("monthly malaria last year", ["LAST_YEAR"], TODAY) == MONTHS_2025


def test_no_granularity_or_already_fine_periods_untouched():
    assert expand_periods("malaria in 2025", ["2025"], TODAY) == ["2025"]
    assert expand_periods("monthly malaria last 12 months", ["LAST_12_MONTHS"], TODAY) == ["LAST_12_MONTHS"]


def test_year_range_fills_gap_without_granularity():
    assert expand_periods("ANC 1 coverage 2022 to 2025", ["2022", "2025"], TODAY) == ["2022", "2023", "2024", "2025"]


def test_both_classifiers_apply_expansion():
    msg = "Monthly trend of inpatient malaria cases in Sierra Leone for 2025"
    assert classify_intent(msg, today=TODAY)["periods"] == MONTHS_2025
    merged = _merge_llm_intent({"periods": ["2025"], "metrics": [{"label": "Inpatient malaria cases"}]},
                               None, TODAY, msg)
    assert merged["periods"] == MONTHS_2025


# --- 2. reporting timeliness wording ---------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("How timely was Child Health reporting in Bo in 2025?", True),
    ("Which districts reported late for Child Health?", True),
    ("How complete was Child Health reporting last quarter?", True),
    ("What is the reporting completeness of the Reproductive Health data set?", True),
    ("Write a programme review report on ANC 1 Coverage", False),
    ("Write a complete report on ANC 1 Coverage", False),
    ("How many malaria cases were reported in Bo?", False),
])
def test_reporting_question_detection(text, expected):
    assert is_reporting_question(text) is expected


def test_timely_wording_maps_to_on_time_rate(monkeypatch):
    import asyncio

    from backend.app.agent.nodes import metadata_resolve as M

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, path, params=None):
            return {"dataSets": [{"id": "BfMAe6Itzgt", "name": "Child Health"},
                                 {"id": "QX4ZTUbOt3a", "name": "Reproductive Health"}]}

    monkeypatch.setattr("backend.app.dhis2.client.DHIS2Client", FakeClient)
    on_time = asyncio.run(M._reporting_rate_metric("How timely was Child Health reporting in Bo?", None))
    plain = asyncio.run(M._reporting_rate_metric("How complete was Child Health reporting in Bo?", None))
    assert on_time["uid"] == "BfMAe6Itzgt.REPORTING_RATE_ON_TIME"
    assert plain["uid"] == "BfMAe6Itzgt.REPORTING_RATE"


# --- 3. tracker questions ---------------------------------------------------

def _state(metrics, org_uid="UNRESOLVED", strategy="tracker_api"):
    return {"metrics": metrics, "org_unit": {"label": "National", "uid": org_uid},
            "data_retrieval_strategy": strategy}


def test_tracker_strategy_uses_analytics_for_program_indicators():
    st = _state([{"uid": "p2Zxg0wcPQ3", "object_type": "programIndicator", "label": "BCG doses"}])
    assert effective_strategy(st) == "analytics_api"


def test_tracker_strategy_never_needs_org_unit_resolution():
    st = _state([{"uid": "UNRESOLVED", "object_type": "indicator", "label": "x"}])
    assert effective_strategy(st) == "analytics_api"
    st = _state([{"uid": "UNRESOLVED", "object_type": "indicator", "label": "x"}], org_uid="O6uvpzGd5pu")
    assert effective_strategy(st) == "tracker_api"


def test_reporting_rate_and_analytics_strategy_untouched():
    st = _state([{"uid": "BfMAe6Itzgt.REPORTING_RATE", "object_type": "reportingRate"}], strategy="data_value_sets")
    assert effective_strategy(st) == "analytics_api"
    assert effective_strategy(_state([], strategy="analytics_api")) == "analytics_api"


def _cand(label, otype, fit, has=True, source=""):
    return {"uid": label[:11].ljust(11, "x"), "label": label, "object_type": otype,
            "user_score": fit, "score": fit, "has_data": has, "source": source}


def test_prefer_program_when_user_names_tracker():
    cands = [_cand("BCG doses given", "dataElement", 0.72),
             _cand("BCG doses", "programIndicator", 0.75, source="Child Programme")]
    v = {"decision": "match", "choice": 1, "alternatives": [2], "candidates": cands, "by": "llm"}
    R.prefer_program(v, "From the Child Programme tracker, how many BCG doses were recorded in 2025?")
    assert v["choice"] == 2 and v["by"].endswith("+program")
    v = {"decision": "match", "choice": 1, "alternatives": [2], "candidates": cands, "by": "llm"}
    R.prefer_program(v, "How many BCG doses were given in 2025?")
    assert v["choice"] == 1


def test_source_words_do_not_make_concept_unknown():
    q = R.content_tokens("From the Child Programme tracker, how many BCG doses were recorded nationally in 2025?")
    assert "tracker" not in q and "nationally" not in q and "2025" not in q and "bcg" in q


# --- 4. count vs coverage ---------------------------------------------------

def test_count_words_prefer_data_element():
    cands = [_cand("FIC <1y", "indicator", 0.1), _cand("Fully Immunized child", "dataElement", 1.0)]
    v = {"decision": "match", "choice": 1, "alternatives": [], "candidates": cands, "by": "llm"}
    R.prefer_measure_type(v, "Fully immunised children by district this year")
    assert v["choice"] == 2 and v["by"].endswith("+count")


def test_rate_words_prefer_indicator():
    cands = [_cand("Penta3 doses given", "dataElement", 0.80), _cand("Penta 3 Coverage <1y", "indicator", 0.86)]
    v = {"decision": "match", "choice": 1, "alternatives": [], "candidates": cands, "by": "rules"}
    R.prefer_measure_type(v, "Monthly Penta 3 coverage for Bo in 2025")
    assert v["choice"] == 2


def test_type_switch_never_trades_for_a_worse_fit_or_empty_item():
    cands = [_cand("Malaria confirmed cases", "indicator", 0.9), _cand("Malaria tested", "dataElement", 0.5)]
    v = {"decision": "match", "choice": 1, "alternatives": [], "candidates": cands, "by": "llm"}
    R.prefer_measure_type(v, "How many malaria confirmed cases in Bo?")
    assert v["choice"] == 1
    cands = [_cand("FIC <1y", "indicator", 0.1), _cand("Fully Immunized child", "dataElement", 1.0, has=False)]
    v = {"decision": "match", "choice": 1, "alternatives": [], "candidates": cands, "by": "llm"}
    R.prefer_measure_type(v, "Fully immunised children by district")
    assert v["choice"] == 1


def test_count_and_rate_word_detection():
    assert R.asks_for_count("How many BCG doses were given?")
    assert R.asks_for_count("Fully immunised children by district")
    assert not R.asks_for_count("BCG coverage among children")
    assert R.asks_for_rate("BCG coverage among children")


def test_british_spelling_matches_american_names():
    assert R.tokens("immunised children") [0] == R.tokens("Immunized child")[0]


def test_named_year_wins_over_lost_parser_period():
    assert expand_periods("Monthly Penta 3 coverage for Bo in 2025", ["THIS_YEAR"], TODAY) == MONTHS_2025
