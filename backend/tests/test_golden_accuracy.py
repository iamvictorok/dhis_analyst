"""Golden test suite — end-to-end metadata resolution accuracy.

Seeds a realistic metadata index with Sierra Leone DHIS2 indicators and
org units, then verifies that natural language queries resolve to the
correct UIDs through the full keyword search cascade.

This is the measurement harness for the NL→DHIS2 accuracy work.
"""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.metadata_index import (
    search_indicator_by_name,
    search_org_unit_by_name,
    get_indicator_names_for_prompt,
)


# ---------------------------------------------------------------------------
# Realistic Sierra Leone DHIS2 metadata fixture
# ---------------------------------------------------------------------------

_INDICATORS = [
    ("ReUHfIn0pTQ", "indicator", "ANC 1st visit", "ANC 1", "Antenatal care first visit"),
    ("AUqdhY4mpvp", "indicator", "ANC 4th or more visits", "ANC 4+", "Antenatal 4th or more visits"),
    ("hCVSHjcml9g", "indicator", "Malaria confirmed cases", "Mal conf", "Malaria confirmed cases by RDT or microscopy"),
    ("nFICjJluo74", "indicator", "Malaria deaths", "Mal deaths", "Deaths attributed to malaria"),
    ("V37YqbqpEhV", "indicator", "Malaria testing rate", "Mal test rate", "Proportion tested for malaria"),
    ("s46m5MS0hxu", "indicator", "BCG doses given", "BCG", "BCG immunization doses administered"),
    ("YtbsuPPo010", "indicator", "OPV3 doses given", "OPV3", "Oral polio vaccine 3rd dose"),
    ("n6aMJNLdvep", "indicator", "Penta3 doses given", "Penta3", "Pentavalent vaccine 3rd dose"),
    ("fbfJHSPpUQD", "indicator", "HIV tests performed", "HIV tests", "Number of HIV tests performed"),
    ("Tt5TAvdfdVK", "indicator", "Maternal mortality cases", "MMR cases", "Maternal mortality cases reported"),
    ("bqK6eSIwo3h", "indicator", "Measles doses given", "MCV1", "Measles vaccine 1st dose"),
    ("GQY2lXnPwz7", "indicator", "COVID-19 cases", "COVID", "Confirmed COVID-19 cases"),
    ("Uvn6LCg7dVU", "dataElement", "TB cases notified", "TB notif", "Tuberculosis cases notified"),
    ("GCyeKSqlpdk", "indicator", "Diarrhoea cases treated", "Diarrhoea", "Diarrhoeal disease cases treated"),
    ("AwDhMYjqOEZ", "indicator", "IPT2 doses given", "IPT2", "Intermittent preventive treatment 2nd dose"),
    ("kHKbGAZy7cS", "indicator", "Vitamin A supplementation", "Vit A", "Vitamin A supplementation coverage"),
    ("l6byfWFUGaP", "indicator", "OPV0 doses given", "OPV0", "Oral polio vaccine birth dose"),
    ("sB79w2hiLp8", "indicator", "Yellow Fever doses given", "YF", "Yellow fever vaccine doses"),
    ("Jtf34kNZhzP", "indicator", "ANC IPTp1 doses given", "IPTp1", "ANC IPT dose 1"),
    ("bCGcKetYPMq", "indicator", "ANC IPTp2 doses given", "IPTp2", "ANC IPT dose 2"),
    ("TkDhg29x18A", "indicator", "Fully immunised children", "Fully imm", "Children fully immunised"),
    ("JoEzWYGdX5K", "indicator", "PMTCT ART coverage", "PMTCT ART", "Prevention of mother-to-child transmission ART"),
    ("pyP5JrHwzQg", "indicator", "Delivery by skilled attendant", "Skilled delivery", "Deliveries attended by skilled health personnel"),
    ("fbqbmQbE4eG", "indicator", "Institutional deliveries", "Inst delivery", "Deliveries in health facilities"),
    ("xUesg8lck3Y", "indicator", "Family planning new acceptors", "FP new", "Family planning new acceptors"),
    ("bCEvnK2dkFY", "indicator", "Postnatal care visit within 48 hours", "PNC 48h", "PNC within 48 hours of delivery"),
    ("FpLgneEdNnL", "indicator", "Low birth weight", "LBW", "Babies with birth weight below 2500g"),
    ("u2aRkLTaKSR", "indicator", "Stillbirths reported", "Stillbirth", "Fresh and macerated stillbirths"),
    ("Y3jfPjWCpTt", "indicator", "Neonatal deaths", "Neo deaths", "Neonatal deaths within 28 days"),
    ("ASc2AMJbhsZ", "indicator", "Under-5 mortality cases", "U5 mort", "Under-five mortality cases"),
]

_ORG_UNITS = [
    ("O6uvpzGd5pu", "orgUnit", "Bo", "Bo", None, '{"level": 2}'),
    ("fdc6uOvgoji", "orgUnit", "Bombali", "Bombali", None, '{"level": 2}'),
    ("kJq2mPyFEHo", "orgUnit", "Kenema", "Kenema", None, '{"level": 2}'),
    ("jUb8gELQApl", "orgUnit", "Kailahun", "Kailahun", None, '{"level": 2}'),
    ("TEQlaapDQoK", "orgUnit", "Port Loko", "Port Loko", None, '{"level": 2}'),
    ("at6UHUQatSo", "orgUnit", "Western Area Urban", "WA Urban", None, '{"level": 2}'),
    ("bL4ooGhyHRQ", "orgUnit", "Western Area Rural", "WA Rural", None, '{"level": 2}'),
    ("PMa2VCrupOd", "orgUnit", "Kambia", "Kambia", None, '{"level": 2}'),
    ("qhqAxPSTUXp", "orgUnit", "Koinadugu", "Koinadugu", None, '{"level": 2}'),
    ("Vth0fbpFcsO", "orgUnit", "Kono", "Kono", None, '{"level": 2}'),
    ("jmIPBj66vD6", "orgUnit", "Moyamba", "Moyamba", None, '{"level": 2}'),
    ("lc3eMKXaEfw", "orgUnit", "Bonthe", "Bonthe", None, '{"level": 2}'),
    ("eIQbndfxQMb", "orgUnit", "Tonkolili", "Tonkolili", None, '{"level": 2}'),
    ("ImspTQPwCqd", "orgUnit", "Sierra Leone", "SL", None, '{"level": 1}'),
]


@pytest_asyncio.fixture
async def golden_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE VIRTUAL TABLE metadata_index_fts USING fts5(
                uid UNINDEXED,
                object_type UNINDEXED,
                name,
                short_name,
                description,
                dataset_names,
                raw_metadata UNINDEXED,
                last_synced_at UNINDEXED
            )
        """))
        for uid, ot, name, short, desc in _INDICATORS:
            await conn.execute(
                text(
                    "INSERT INTO metadata_index_fts "
                    "(uid, object_type, name, short_name, description) "
                    "VALUES (:u,:o,:n,:s,:d)"
                ),
                {"u": uid, "o": ot, "n": name, "s": short, "d": desc},
            )
        for uid, ot, name, short, desc, raw in _ORG_UNITS:
            await conn.execute(
                text(
                    "INSERT INTO metadata_index_fts "
                    "(uid, object_type, name, short_name, description, raw_metadata) "
                    "VALUES (:u,:o,:n,:s,:d,:r)"
                ),
                {"u": uid, "o": ot, "n": name, "s": short, "d": desc, "r": raw},
            )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Indicator resolution tests — natural language -> correct UID
# ---------------------------------------------------------------------------

_INDICATOR_CASES = [
    # (query, expected_uid, description)
    ("Malaria confirmed cases", "hCVSHjcml9g", "exact match"),
    ("ANC 1st visit", "ReUHfIn0pTQ", "exact ANC 1"),
    ("ANC 4th or more visits", "AUqdhY4mpvp", "exact ANC 4+"),
    ("OPV3 doses given", "YtbsuPPo010", "exact OPV3"),
    ("BCG doses given", "s46m5MS0hxu", "exact BCG"),
    ("HIV tests performed", "fbfJHSPpUQD", "exact HIV tests"),
    ("Malaria deaths", "nFICjJluo74", "exact malaria deaths"),
    ("Maternal mortality cases", "Tt5TAvdfdVK", "exact maternal mortality"),
    ("malaria confirmed cases", "hCVSHjcml9g", "case-insensitive exact"),
    ("Penta3 doses given", "n6aMJNLdvep", "exact Penta3"),
    ("COVID-19 cases", "GQY2lXnPwz7", "exact COVID"),
    ("TB cases notified", "Uvn6LCg7dVU", "dataElement type"),
    ("Diarrhoea cases treated", "GCyeKSqlpdk", "exact diarrhoea"),
    ("Neonatal deaths", "Y3jfPjWCpTt", "exact neonatal"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected_uid,desc", _INDICATOR_CASES, ids=[c[2] for c in _INDICATOR_CASES])
async def test_indicator_resolves_correctly(golden_session, query, expected_uid, desc):
    results = await search_indicator_by_name(golden_session, query, limit=5)
    assert len(results) > 0, f"No results for '{query}'"
    assert results[0]["uid"] == expected_uid, (
        f"[{desc}] Expected '{expected_uid}' but got '{results[0]['uid']}' "
        f"(label='{results[0]['label']}', conf={results[0]['uid_confidence']})"
    )


# ---------------------------------------------------------------------------
# Indicator confidence scoring tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exact_match_scores_095(golden_session):
    results = await search_indicator_by_name(golden_session, "Malaria confirmed cases", limit=5)
    assert results[0]["uid_confidence"] >= 0.95


@pytest.mark.asyncio
async def test_partial_match_scores_below_exact(golden_session):
    results = await search_indicator_by_name(golden_session, "malaria", limit=5)
    # Bare single-token hit should score meaningfully below exact match (0.95)
    assert all(r["uid_confidence"] < 0.90 for r in results)


@pytest.mark.asyncio
async def test_token_coverage_improves_confidence(golden_session):
    """More query tokens matching -> higher confidence."""
    results_full = await search_indicator_by_name(golden_session, "Malaria confirmed cases", limit=5)
    results_partial = await search_indicator_by_name(golden_session, "malaria", limit=5)
    assert results_full[0]["uid_confidence"] > results_partial[0]["uid_confidence"]


# ---------------------------------------------------------------------------
# search_alternatives (LLM-provided fallback terms)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_alternatives_resolve_synonym(golden_session):
    """LLM-provided search_alternatives should find indicators the primary label misses."""
    results = await search_indicator_by_name(
        golden_session, "childhood vaccination coverage",
        limit=5,
        search_alternatives=["immunization", "doses given", "OPV3"],
    )
    assert len(results) > 0
    # Should resolve to one of the immunization indicators
    assert results[0]["uid"] in {"s46m5MS0hxu", "YtbsuPPo010", "n6aMJNLdvep", "TkDhg29x18A"}


# ---------------------------------------------------------------------------
# Org unit resolution tests
# ---------------------------------------------------------------------------

_ORG_UNIT_CASES = [
    ("Bo", "O6uvpzGd5pu", "exact Bo"),
    ("Kenema", "kJq2mPyFEHo", "exact Kenema"),
    ("Kailahun", "jUb8gELQApl", "exact Kailahun"),
    ("Port Loko", "TEQlaapDQoK", "exact Port Loko"),
    ("Sierra Leone", "ImspTQPwCqd", "national level"),
    ("Bombali", "fdc6uOvgoji", "exact Bombali"),
    ("Kambia", "PMa2VCrupOd", "exact Kambia"),
    ("bo", "O6uvpzGd5pu", "case-insensitive Bo"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected_uid,desc", _ORG_UNIT_CASES, ids=[c[2] for c in _ORG_UNIT_CASES])
async def test_org_unit_resolves_correctly(golden_session, query, expected_uid, desc):
    results = await search_org_unit_by_name(golden_session, query, limit=3)
    assert len(results) > 0, f"No results for '{query}'"
    assert results[0]["uid"] == expected_uid, (
        f"[{desc}] Expected '{expected_uid}' but got '{results[0]['uid']}' "
        f"(label='{results[0]['label']}')"
    )


# ---------------------------------------------------------------------------
# Indicator catalogue for prompt injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_indicator_catalogue_returns_names(golden_session):
    catalogue = await get_indicator_names_for_prompt(golden_session, is_postgres=False)
    assert len(catalogue) > 0
    lines = catalogue.strip().split("\n")
    # Should contain at least our seeded indicators
    assert len(lines) >= 20
    # Names should be sorted by length
    lengths = [len(line) for line in lines]
    assert lengths == sorted(lengths)


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_queries_are_deterministic(golden_session):
    first = [r["uid"] for r in await search_indicator_by_name(golden_session, "malaria", limit=5)]
    second = [r["uid"] for r in await search_indicator_by_name(golden_session, "malaria", limit=5)]
    assert first == second


@pytest.mark.asyncio
async def test_no_results_for_nonsense(golden_session):
    results = await search_indicator_by_name(golden_session, "xyzzy foobar baz", limit=5)
    assert results == []
