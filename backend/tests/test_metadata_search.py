"""Metadata index search — deterministic ordering and graded confidence.

These lock in the fixes for the "same query resolves to a different indicator
after each sync" class of intermittency: results must be ordered deterministically
and weak substring matches must score below the disambiguation threshold.
"""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.metadata_index import search_indicator_by_name


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE metadata_index_lite (
                uid TEXT PRIMARY KEY, object_type TEXT NOT NULL, name TEXT NOT NULL,
                short_name TEXT, description TEXT, dataset_names TEXT,
                raw_metadata TEXT, last_synced_at TEXT
            )
        """))
        rows = [
            ("uid_deaths00", "indicator", "Malaria deaths"),
            ("uid_cases0000", "indicator", "Malaria cases"),
            ("uid_testrate0", "indicator", "Malaria testing rate"),
            ("uid_confirmed", "indicator", "Malaria confirmed cases reported at facilities"),
        ]
        for uid, ot, name in rows:
            await conn.execute(
                text("INSERT INTO metadata_index_lite (uid, object_type, name) VALUES (:u,:o,:n)"),
                {"u": uid, "o": ot, "n": name},
            )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_exact_match_ranks_first_and_scores_high(session):
    results = await search_indicator_by_name(session, "Malaria cases", limit=5)
    assert results[0]["label"] == "Malaria cases"
    assert results[0]["uid_confidence"] >= 0.95


@pytest.mark.asyncio
async def test_ordering_is_deterministic(session):
    # Repeated identical queries must return the same ranking every time.
    first = [r["uid"] for r in await search_indicator_by_name(session, "malaria", limit=5)]
    second = [r["uid"] for r in await search_indicator_by_name(session, "malaria", limit=5)]
    assert first == second
    # Shortest (most specific) name wins the substring race, not an arbitrary row.
    assert first[0] == "uid_cases0000"


@pytest.mark.asyncio
async def test_weak_substring_match_scores_below_threshold(session):
    # A bare substring hit (no exact-name match anywhere) must stay below 0.82 so
    # it flows into disambiguation instead of being silently accepted.
    results = await search_indicator_by_name(session, "malaria", limit=5)
    assert all(r["uid_confidence"] < 0.82 for r in results)


@pytest.mark.asyncio
async def test_like_wildcards_are_escaped(session):
    # A stray '%' must be matched literally, not as "match everything".
    results = await search_indicator_by_name(session, "100%", limit=5)
    assert results == []
