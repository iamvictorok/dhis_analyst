"""Deterministic facts handed to the LLM so it never does its own arithmetic."""
from backend.app.agent.nodes.generate_content import _data_facts


def _state(rows, pe_ids=None, items=None):
    return {
        "metrics": [{"label": "ANC 1 Coverage", "uid": "Uvn6LCg7dVU"}],
        "dhis2_data": {
            "rows": rows,
            "metadata": {"raw_metadata": {"dimensions": {"pe": pe_ids or []}, "items": items or {}}},
        },
    }


def test_series_stats_sorted_chronologically():
    rows = [
        ["Sierra Leone", "December 2025", "79.37", "ANC 1 Coverage"],
        ["Sierra Leone", "October 2025", "86.52", "ANC 1 Coverage"],
        ["Sierra Leone", "November 2025", "98.21", "ANC 1 Coverage"],
    ]
    items = {"202510": {"name": "October 2025"}, "202511": {"name": "November 2025"}, "202512": {"name": "December 2025"}}
    out = _data_facts(_state(rows, ["202510", "202511", "202512"], items))
    assert "highest 98.21 (November 2025)" in out
    assert "lowest 79.37 (December 2025)" in out
    assert "mean 88.03" in out
    assert "first 86.52 (October 2025) -> last 79.37 (December 2025) = -8.3%" in out
    assert "Uvn6LCg7dVU" in out


def test_ranking_for_one_period_many_org_units():
    rows = [
        ["Bo", "2025", "6656", "Penta3 doses given"],
        ["Western Area", "2025", "25119", "Penta3 doses given"],
        ["Kenema", "2025", "6494", "Penta3 doses given"],
    ]
    out = _data_facts(_state(rows))
    assert "1. Western Area 25,119; 2. Bo 6,656; 3. Kenema 6,494" in out


def test_empty_rows():
    assert _data_facts(_state([])) == ""
