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


def test_normalise_analytics_response_reorders_to_canonical_layout():
    # Real DHIS2 returns columns in requested-dimension order (dx, ou, pe) with
    # the value last. Normalisation must reorder into [org, period, value, metric]
    # so the renderers (which assume the mock layout) compute correct numbers.
    raw = {
        "headers": [
            {"name": "dx", "column": "Data"},
            {"name": "ou", "column": "Organisation unit"},
            {"name": "pe", "column": "Period"},
            {"name": "value", "column": "Value"},
        ],
        "rows": [
            ["ANC 1st visit", "Bo", "2025Q1", "1200"],
            ["ANC 1st visit", "Bo", "2025Q2", "1350"],
        ],
        "metaData": {"items": {}},
    }

    result = normalise_analytics_response(raw, {"dimension": []})

    assert result["headers"] == ["Organisation unit", "Period", "Value", "Metric"]
    assert result["rows"] == [
        ["Bo", "2025Q1", "1200", "ANC 1st visit"],
        ["Bo", "2025Q2", "1350", "ANC 1st visit"],
    ]
    assert result["metadata"]["data_source"] == "analytics_api"


def test_normalise_analytics_response_handles_missing_period_column():
    raw = {
        "headers": [{"column": "dx"}, {"column": "ou"}, {"column": "value"}],
        "rows": [["ANC 1st visit", "Bo", "12"]],
        "metaData": {"items": {}},
    }

    result = normalise_analytics_response(raw, {"dimension": []})

    # dx->metric, ou->org, value->value, no period column present
    assert result["rows"] == [["Bo", "", "12", "ANC 1st visit"]]


def test_build_analytics_params_drops_invalid_disaggregation_tokens():
    params = build_analytics_params(_state(disaggregations=["sex", "age"]))
    # Free-text tokens are not valid category-option-combo UIDs and must not be
    # sent as a co: dimension (DHIS2 would 409).
    assert not any(d.startswith("co:") for d in params["dimension"])


def test_build_analytics_params_national_children_uses_user_orgunit_level():
    params = build_analytics_params(
        _state(
            org_unit={"label": "National", "uid": "UNRESOLVED", "level": 1},
            disaggregations=["ou_children"],
        )
    )
    assert "ou:USER_ORGUNIT;LEVEL-2" in params["dimension"]


def test_build_analytics_params_unresolved_named_org_falls_back_to_user_orgunit():
    params = build_analytics_params(
        _state(org_unit={"label": "Bojo District", "uid": "UNRESOLVED", "level": 3})
    )
    # A named-but-unresolved org unit must never be sent as ou:UNRESOLVED.
    assert "ou:USER_ORGUNIT" in params["dimension"]
    assert not any("UNRESOLVED" in d for d in params["dimension"])
