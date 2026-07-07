"""Tests for the LLM-assisted intent path — the shape every real request takes.

Covers the defensive normalisation in _merge_llm_intent (malformed/hostile LLM
JSON must degrade gracefully, never raise) and the deterministic-fallback regex
fixes that feed it when the LLM call fails.
"""
from datetime import date

import pytest

from backend.app.agent import intent

TODAY = date(2026, 7, 7)


def merge(parsed, message="show me the data"):
    return intent._merge_llm_intent(parsed, None, TODAY, message)


def test_null_org_unit_label_does_not_crash():
    result = merge({"org_unit_label": None, "metrics": [{"label": "malaria"}], "periods": ["THIS_YEAR"]})
    assert result["org_unit"]["label"] == "National"


def test_nested_org_unit_label_object():
    result = merge({"org_unit_label": {"label": "Kano"}, "metrics": [{"label": "x"}], "periods": ["THIS_YEAR"]})
    assert result["org_unit"]["label"] == "Kano"


def test_integer_period_is_coerced_not_crashed():
    result = merge({"metrics": [{"label": "malaria"}], "org_unit_label": "Bo", "periods": [2024]})
    assert result["periods"] == ["2024"]


def test_bare_string_period_is_wrapped():
    result = merge({"metrics": [{"label": "x"}], "org_unit_label": "National", "periods": "THIS_YEAR"})
    assert result["periods"] == ["THIS_YEAR"]


def test_metrics_as_list_of_strings():
    result = merge({"metrics": ["ANC 4th visit coverage"], "org_unit_label": "Bo", "periods": ["2024"]})
    assert result["metrics"][0]["label"] == "ANC 4th visit coverage"


def test_metrics_as_single_dict():
    result = merge({"metrics": {"label": "measles"}, "org_unit_label": "Bo", "periods": ["2024"]})
    assert result["metrics"][0]["label"] == "measles"


def test_null_metric_label_is_skipped_and_defaulted():
    result = merge({"metrics": [{"label": None}], "org_unit_label": "Bo", "periods": ["2024"]})
    # Falls back to the default metric rather than carrying a null label forward.
    assert isinstance(result["metrics"][0]["label"], str)
    assert result["metrics"][0]["label"].strip()


def test_unknown_output_mode_is_coerced_to_conversational():
    result = merge({"output_mode": "visualization", "metrics": [{"label": "x"}],
                    "org_unit_label": "National", "periods": ["THIS_YEAR"]})
    assert result["output_mode"] == "conversational"


def test_map_output_mode_is_accepted():
    result = merge({"output_mode": "map", "metrics": [{"label": "x"}],
                    "org_unit_label": "Kaduna", "periods": ["THIS_YEAR"]})
    assert result["output_mode"] == "map"


def test_llm_supplied_uid_is_not_trusted():
    # A syntactically valid but unverified UID must be marked for resolution.
    result = merge({"metrics": [{"label": "malaria", "uid": "aBcDeFgHiJ1"}],
                    "org_unit_label": "Bo", "periods": ["2024"]})
    assert result["metrics"][0]["uid_confidence"] < 0.82


def test_viz_types_honour_requested_pie_chart():
    result = merge(
        {"output_mode": "dashboard", "metrics": [{"label": "x"}], "org_unit_label": "Bo", "periods": ["2024"]},
        message="show me a pie chart of ANC coverage",
    )
    assert "pie" in result["viz_types"]


# --- deterministic fallback regex fixes ---

def test_metric_label_word_boundary_stripping():
    # "state" must not gut "prostate".
    label = intent._extract_metric_labels("prostate cancer screening rate this year")[0]["label"]
    assert "prostate" in label


def test_org_unit_skips_month_names():
    org = intent._extract_org_unit("Malaria cases in March 2024 for Kano")
    assert org["label"] == "Kano"


def test_org_unit_skips_metric_acronym_and_finds_place():
    org = intent._extract_org_unit("Trend of BCG coverage in Kano")
    assert org["label"] == "Kano"


def test_web_enrichment_word_boundary():
    assert intent._needs_web_enrichment("whooping cough cases in kano") is False
    assert intent._needs_web_enrichment("what does WHO recommend") is True


def test_quantity_not_treated_as_year():
    periods = intent._detect_periods("districts with over 2000 malaria cases", TODAY)
    assert "2000" not in periods


def test_real_year_is_detected():
    assert "2024" in intent._detect_periods("malaria cases in 2024", TODAY)
