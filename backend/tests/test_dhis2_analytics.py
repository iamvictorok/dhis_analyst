"""DHIS2 analytics query builder tests."""

from backend.app.dhis2.analytics import build_analytics_params, normalise_analytics_response


def _state(**overrides):
    state = {
        "metrics": [
            {"label": "ANC 1st visit", "uid": "fbfJHSPpUQD", "uid_confidence": 0.95, "object_type": "dataElement"},
            {"label": "ANC 4th visit", "uid": "cYeuwXTCPkU", "uid_confidence": 0.95, "object_type": "dataElement"},
        ],
        "org_unit": {"label": "Bo", "uid": "O6uvpzGd5pu", "level": 2},
        "periods": ["2025Q1", "2025Q2"],
        "disaggregations": [],
    }
    state.update(overrides)
    return state


def test_build_analytics_params_uses_dhis2_semicolon_item_separator():
    params = build_analytics_params(_state())

    assert "dx:fbfJHSPpUQD;cYeuwXTCPkU" in params["dimension"]
    assert "pe:2025Q1;2025Q2" in params["dimension"]
    assert "ou:O6uvpzGd5pu" in params["dimension"]


def test_build_analytics_params_scopes_child_org_units_to_boundary():
    params = build_analytics_params(_state(disaggregations=["ou_children"]))

    assert "ou:LEVEL-3;O6uvpzGd5pu" in params["dimension"]


def test_build_analytics_params_uses_user_org_unit_for_unresolved_national():
    params = build_analytics_params(
        _state(org_unit={"label": "National", "uid": "UNRESOLVED", "level": 1})
    )

    assert "ou:USER_ORGUNIT" in params["dimension"]


def test_normalise_analytics_response_preserves_rows_and_headers():
    raw = {
        "headers": [{"column": "dx"}, {"column": "ou"}, {"column": "value"}],
        "rows": [["ANC 1st visit", "Bo", "12"]],
        "metaData": {"items": {}},
    }

    result = normalise_analytics_response(raw, {"dimension": []})

    assert result["headers"] == ["dx", "ou", "value"]
    assert result["rows"] == [["ANC 1st visit", "Bo", "12"]]
    assert result["metadata"]["data_source"] == "analytics_api"
