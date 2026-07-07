"""DHIS2 client tests — metadata pagination."""
from unittest.mock import patch

import pytest

from backend.config import Settings
from backend.app.dhis2.client import DHIS2Client


def _client():
    return DHIS2Client(Settings(llm_provider="mock"))


@pytest.mark.asyncio
async def test_metadata_follows_pager_until_exhausted():
    client = _client()
    pages = {
        1: {"organisationUnits": [{"id": "a"}, {"id": "b"}],
            "pager": {"page": 1, "pageCount": 3, "nextPage": "http://x?page=2"}},
        2: {"organisationUnits": [{"id": "c"}],
            "pager": {"page": 2, "pageCount": 3, "nextPage": "http://x?page=3"}},
        3: {"organisationUnits": [{"id": "d"}],
            "pager": {"page": 3, "pageCount": 3}},  # no nextPage -> stop
    }
    requested = []

    async def fake_get(path, params=None):
        requested.append(params["page"])
        return pages[params["page"]]

    with patch.object(client, "_get", side_effect=fake_get):
        items = await client.metadata("organisationUnits")

    assert requested == [1, 2, 3]
    assert [i["id"] for i in items] == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_metadata_stops_when_server_ignores_paging():
    client = _client()

    async def fake_get(path, params=None):
        # No pager in the response — must not loop forever.
        return {"indicators": [{"id": "x"}, {"id": "y"}]}

    with patch.object(client, "_get", side_effect=fake_get):
        items = await client.metadata("indicators")

    assert [i["id"] for i in items] == ["x", "y"]


@pytest.mark.asyncio
async def test_metadata_respects_max_pages_cap():
    client = _client()

    async def fake_get(path, params=None):
        # Always claims another page — the cap must break the loop.
        return {"dataElements": [{"id": str(params["page"])}],
                "pager": {"nextPage": "http://x"}}

    with patch.object(client, "_get", side_effect=fake_get):
        items = await client.metadata("dataElements", max_pages=5)

    assert len(items) == 5
