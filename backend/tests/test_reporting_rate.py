"""Data set reporting rates go into dx as <dataSet>.REPORTING_RATE."""
from backend.app.dhis2.analytics import build_analytics_params


def test_reporting_rate_dx_item_accepted():
    state = {
        "metrics": [{"uid": "BfMAe6Itzgt.REPORTING_RATE", "object_type": "reportingRate", "label": "Child Health - Reporting rate"}],
        "org_unit": {"uid": "ImspTQPwCqd", "label": "Sierra Leone", "level": 1},
        "disaggregations": ["ou_children"],
        "periods": ["LAST_QUARTER"],
    }
    params = build_analytics_params(state)
    assert "dx:BfMAe6Itzgt.REPORTING_RATE" in params["dimension"]
    assert "ou:LEVEL-2;ImspTQPwCqd" in params["dimension"]


def test_reporting_rate_suffix_must_be_known():
    state = {
        "metrics": [
            {"uid": "BfMAe6Itzgt.DROP_TABLE", "object_type": "reportingRate", "label": "bad"},
            {"uid": "Uvn6LCg7dVU", "object_type": "indicator", "label": "ANC 1 Coverage"},
        ],
        "org_unit": {"uid": "ImspTQPwCqd", "label": "Sierra Leone", "level": 1},
        "disaggregations": [],
        "periods": ["2025"],
    }
    params = build_analytics_params(state)
    assert params["dimension"][0] == "dx:Uvn6LCg7dVU"
