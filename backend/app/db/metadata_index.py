"""Metadata index — schema definition, upsert, and search functions.

Supports:
- pgvector cosine similarity search (Postgres)
- Multi-strategy keyword search (SQLite FTS5 + Postgres LIKE)
- Health domain synonym expansion for layperson language
"""
from __future__ import annotations

import logging
import re as _re
from typing import Any

logger = logging.getLogger("dhis2_analyst.metadata_index")


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so user/LLM text is matched literally.

    Without this, a ``%`` in the term matches every row and ``_`` matches any
    character, which — combined with LIMIT — returns an arbitrary row as a
    "confident" match. Paired with ``ESCAPE '\'`` in the SQL.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Health domain synonym map & search helpers
# ---------------------------------------------------------------------------

# Layperson term → DHIS2-common search terms.  Used to bridge the gap between
# how a user describes a health concept and how it appears in DHIS2 metadata.
_HEALTH_SYNONYMS: dict[str, list[str]] = {
    "shots": ["immunization", "vaccination", "doses", "vaccine"],
    "jabs": ["immunization", "vaccination", "doses"],
    "vaccines": ["immunization", "vaccination", "doses"],
    "vaccine": ["immunization", "vaccination", "doses"],
    "immunisation": ["immunization"],
    "pregnant": ["antenatal", "ANC", "maternal"],
    "pregnancy": ["antenatal", "ANC", "maternal"],
    "prenatal": ["antenatal", "ANC"],
    "babies": ["neonatal", "infant", "newborn", "child"],
    "baby": ["neonatal", "infant", "newborn", "child"],
    "kids": ["child", "children", "paediatric", "pediatric"],
    "deaths": ["mortality", "death"],
    "dying": ["mortality", "death"],
    "died": ["mortality", "death"],
    "spread": ["incidence", "transmission", "cases"],
    "sick": ["morbidity", "cases"],
    "testing": ["test", "tested", "screening"],
    "tests": ["test", "tested", "screening"],
    "birth": ["delivery", "deliveries", "natal"],
    "births": ["delivery", "deliveries", "natal"],
    "fever": ["febrile"],
    "cough": ["respiratory", "ARI", "pneumonia"],
    "nutrition": ["malnutrition", "stunting", "wasting"],
    "water": ["WASH", "sanitation"],
    "mosquito": ["malaria", "vector"],
    "tb": ["tuberculosis"],
    "tuberculosis": ["TB"],
    "hiv": ["HIV", "AIDS", "ART", "PMTCT"],
    "aids": ["HIV", "AIDS", "ART"],
    "covid": ["COVID-19", "coronavirus"],
    "flu": ["influenza"],
    "diarrhea": ["diarrhoea", "diarrheal"],
    "diarrhoea": ["diarrhea", "diarrheal"],
    "polio": ["OPV", "IPV", "poliomyelitis"],
    "measles": ["measles", "MCV"],
    "maternal": ["maternal", "ANC", "antenatal", "PMTCT"],
    "anc": ["antenatal", "ANC", "maternal"],
    "opv": ["OPV", "polio", "oral polio"],
    "bcg": ["BCG", "tuberculosis vaccine"],
    "penta": ["pentavalent", "Penta", "DPT"],
}

# FTS5 keywords that break MATCH syntax if used as bare tokens
_FTS5_RESERVED = frozenset({"AND", "OR", "NOT", "NEAR"})

# Words too generic to help indicator search
_SEARCH_FILLER = frozenset({
    "the", "a", "an", "of", "in", "for", "and", "or", "by", "to", "is",
    "are", "was", "were", "show", "me", "give", "get", "what", "how",
    "many", "much", "can", "you", "please", "last", "this", "that",
    "those", "these", "all", "data", "numbers", "figures", "statistics",
    "over", "between", "from", "about", "more", "less", "than",
})


def _sanitize_fts_token(token: str) -> str:
    """Strip FTS5 special syntax characters from a search token."""
    cleaned = _re.sub(r'["\(\)\*\+\-\^:{}]', '', token).strip()
    if not cleaned or cleaned.upper() in _FTS5_RESERVED or len(cleaned) < 2:
        return ""
    return cleaned


def _extract_search_tokens(text: str) -> list[str]:
    """Split a search phrase into meaningful tokens for DB search."""
    raw = _re.sub(r'[^\w\s\-]', ' ', text)
    tokens = raw.split()
    meaningful = [t for t in tokens if t.lower() not in _SEARCH_FILLER and len(t) >= 2]
    return meaningful or [t for t in tokens if len(t) >= 2]


def _expand_with_synonyms(tokens: list[str]) -> list[list[str]]:
    """Generate alternative token lists by replacing layperson terms.

    Each returned list replaces ONE token with one of its synonyms.
    Capped at 3 synonyms per token to avoid explosion.
    """
    alternatives: list[list[str]] = []
    for i, token in enumerate(tokens):
        syns = _HEALTH_SYNONYMS.get(token.lower())
        if syns:
            for syn in syns[:3]:
                alt = list(tokens)
                alt[i] = syn
                alternatives.append(alt)
    return alternatives


def _build_indicator_results(
    rows, original_query: str, base_confidence: float,
) -> list[dict[str, Any]]:
    """Score and format indicator search results with quality-graded confidence."""
    results = []
    query_lower = original_query.lower()
    for row in rows:
        name_lower = row[1].lower()
        if name_lower == query_lower:
            confidence = 0.95
        elif query_lower in name_lower or name_lower in query_lower:
            confidence = max(base_confidence, 0.88)
        else:
            confidence = base_confidence
        results.append({
            "uid": row[0],
            "label": row[1],
            "object_type": row[2],
            "short_name": row[3],
            "description": row[4],
            "uid_confidence": confidence,
        })
    return results


# ---------------------------------------------------------------------------
# Public search functions
# ---------------------------------------------------------------------------

async def search_by_embedding(
    session,
    embedding: list[float],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search metadata by cosine similarity using pgvector."""
    from sqlalchemy import text

    result = await session.execute(
        text("""
            SELECT uid, name, object_type, short_name, description,
                   1 - (embedding <=> :emb::vector) AS similarity
            FROM metadata_index
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> :emb::vector
            LIMIT :lim
        """),
        {"emb": str(embedding), "lim": limit},
    )
    return [
        {
            "uid": row[0],
            "label": row[1],
            "object_type": row[2],
            "short_name": row[3],
            "description": row[4],
            "uid_confidence": round(float(row[5]), 4),
        }
        for row in result.fetchall()
    ]


async def search_by_keyword(
    session,
    term: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search metadata by keyword match (SQLite compatible)."""
    from sqlalchemy import text

    # Escape quotes for FTS match
    clean_term = term.replace('"', '""')
    result = await session.execute(
        text(r"""
            SELECT uid, name, object_type, short_name, description
            FROM metadata_index_fts
            WHERE metadata_index_fts MATCH :term
            ORDER BY rank
            LIMIT :lim
        """),
        {"term": f'"{clean_term}"*', "lim": limit},
    )
    return [
        {
            "uid": row[0],
            "label": row[1],
            "object_type": row[2],
            "short_name": row[3],
            "description": row[4],
            "uid_confidence": 0.95 if row[1].lower() == term.lower() else 0.70,
        }
        for row in result.fetchall()
    ]


async def search_org_unit_by_name(
    session,
    name: str,
    *,
    is_postgres: bool = False,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search organisation units by name in the synced metadata index.

    Tries exact match and LIKE match, then cleans suffixes (e.g. "District", "State")
    and retries to ensure robust resolution.
    Works with both PostgreSQL (metadata_index) and SQLite (metadata_index_fts).
    """
    import json
    from sqlalchemy import text

    table = "metadata_index" if is_postgres else "metadata_index_fts"

    def clean_name(n: str) -> str:
        # Strip common qualifiers
        cleaned = _re.sub(
            r"\b(district|lga|ward|facility|state|county|province|country|clinic|hospital|chc|chp|mchp|health\s+center|health\s+post)\b",
            "",
            n,
            flags=_re.IGNORECASE
        )
        return cleaned.strip()

    search_terms = [name]
    cleaned = clean_name(name)
    if cleaned and cleaned.lower() != name.lower():
        search_terms.append(cleaned)

    rows = []
    for term in search_terms:
        # 1. Try exact case-insensitive match
        result = await session.execute(
            text(f"""
                SELECT uid, name, short_name, raw_metadata
                FROM {table}
                WHERE object_type = 'orgUnit'
                  AND LOWER(name) = LOWER(:name)
                ORDER BY uid
                LIMIT :lim
            """),
            {"name": term, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            break

        # 2. Fall back to pattern match (shortest/most-specific name first)
        if is_postgres:
            pattern = f"%{_escape_like(term)}%"
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, short_name, raw_metadata
                    FROM {table}
                    WHERE object_type = 'orgUnit'
                      AND (LOWER(name) LIKE LOWER(:pattern) ESCAPE '\'
                           OR LOWER(short_name) LIKE LOWER(:pattern) ESCAPE '\')
                    ORDER BY LENGTH(name), uid
                    LIMIT :lim
                """),
                {"pattern": pattern, "lim": limit},
            )
        else:
            # Token-AND search: all words must match in any order (instead of
            # rigid phrase matching that fails on word-order mismatches)
            tokens = [_sanitize_fts_token(t) for t in term.split()]
            tokens = [t for t in tokens if t]
            match_term = " ".join(tokens) if tokens else f'"{term.replace(chr(34), "")}"'
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, short_name, raw_metadata
                    FROM {table}
                    WHERE object_type = 'orgUnit'
                      AND metadata_index_fts MATCH :match_term
                    ORDER BY rank, LENGTH(name), uid
                    LIMIT :lim
                """),
                {"match_term": match_term, "lim": limit},
            )
        rows = result.fetchall()
        if rows:
            break

    # --- Fallback: try just the first word (e.g. "Western Area Urban" → "Western") ---
    if not rows:
        first_word = name.split()[0] if name.split() else ""
        if first_word and len(first_word) >= 3 and first_word.lower() != name.lower():
            if is_postgres:
                pattern = f"%{_escape_like(first_word)}%"
                result = await session.execute(
                    text(rf"""
                        SELECT uid, name, short_name, raw_metadata
                        FROM {table}
                        WHERE object_type = 'orgUnit'
                          AND LOWER(name) LIKE LOWER(:pattern) ESCAPE '\'
                        ORDER BY LENGTH(name), uid
                        LIMIT :lim
                    """),
                    {"pattern": pattern, "lim": limit},
                )
            else:
                san = _sanitize_fts_token(first_word)
                if san:
                    result = await session.execute(
                        text(rf"""
                            SELECT uid, name, short_name, raw_metadata
                            FROM {table}
                            WHERE object_type = 'orgUnit'
                              AND metadata_index_fts MATCH :mt
                            ORDER BY rank, LENGTH(name), uid
                            LIMIT :lim
                        """),
                        {"mt": san, "lim": limit},
                    )
                else:
                    result = None
            if result:
                rows = result.fetchall()

    results = []
    for row in rows:
        raw = row[3]
        level = 1
        if raw:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                level = parsed.get("level", 1)
            except (json.JSONDecodeError, TypeError):
                pass
        results.append({
            "uid": row[0],
            "label": row[1],
            "short_name": row[2],
            "level": level,
        })
    return results


async def search_indicator_by_name(
    session,
    name: str,
    *,
    is_postgres: bool = False,
    limit: int = 5,
    search_alternatives: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search indicators/dataElements by name — multi-strategy cascade.

    Strategies tried in order (first match wins):
    1. Exact case-insensitive name match → 0.95 confidence
    2. Token-AND: all search words present in any order → 0.85
    3. Synonym-expanded token search → 0.80
    4. LLM-provided search_alternatives → 0.80
    5. Any single meaningful token match → 0.72
    """
    from sqlalchemy import text

    table = "metadata_index" if is_postgres else "metadata_index_fts"
    obj_types = "object_type IN ('indicator', 'dataElement', 'programIndicator')"

    # --- Build ordered search terms ---
    all_terms = [name]
    # Conservative cleaning: only strip modifiers never found in indicator names
    cleaned = _re.sub(
        r'\b(coverage rate|overall|general|numbers?|figures?|statistics?)\b',
        '', name, flags=_re.IGNORECASE,
    ).strip()
    if cleaned and cleaned.lower() != name.lower() and len(cleaned) >= 3:
        all_terms.append(cleaned)
    if search_alternatives:
        for alt in search_alternatives:
            alt_s = alt.strip()
            if alt_s and alt_s.lower() not in {t.lower() for t in all_terms}:
                all_terms.append(alt_s)

    # -------------------------------------------------------------------
    # Strategy 1: Exact case-insensitive match
    # -------------------------------------------------------------------
    for term in all_terms:
        result = await session.execute(
            text(f"""
                SELECT uid, name, object_type, short_name, description
                FROM {table}
                WHERE {obj_types} AND LOWER(name) = LOWER(:name)
                ORDER BY LENGTH(name), uid
                LIMIT :lim
            """),
            {"name": term, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            return _build_indicator_results(rows, name, 0.95)

    # -------------------------------------------------------------------
    # Strategy 2: Token-AND (all words, any order) — main workhorse
    # -------------------------------------------------------------------
    for term in all_terms:
        tokens = _extract_search_tokens(term)
        if not tokens:
            continue

        if is_postgres:
            # Multi-ILIKE: name must contain ALL tokens
            conds = " AND ".join(
                f"(LOWER(name) LIKE LOWER(:t{i}) OR LOWER(short_name) LIKE LOWER(:t{i}))"
                for i in range(len(tokens))
            )
            params: dict[str, Any] = {
                f"t{i}": f"%{_escape_like(t)}%" for i, t in enumerate(tokens)
            }
            params["lim"] = limit
            result = await session.execute(
                text(f"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE {obj_types} AND {conds}
                    ORDER BY LENGTH(name), uid
                    LIMIT :lim
                """),
                params,
            )
        else:
            fts_tokens = [_sanitize_fts_token(t) for t in tokens]
            fts_tokens = [t for t in fts_tokens if t]
            if not fts_tokens:
                continue
            # FTS5 implicit AND: space-separated tokens match in any order
            match_term = " ".join(fts_tokens)
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE {obj_types}
                      AND metadata_index_fts MATCH :mt
                    ORDER BY rank, LENGTH(name), uid
                    LIMIT :lim
                """),
                {"mt": match_term, "lim": limit},
            )
        rows = result.fetchall()
        if rows:
            return _build_indicator_results(rows, name, 0.85)

    # -------------------------------------------------------------------
    # Strategy 3: Synonym-expanded search
    # -------------------------------------------------------------------
    primary_tokens = _extract_search_tokens(name)
    syn_groups = _expand_with_synonyms(primary_tokens or name.split())
    for syn_tokens in syn_groups:
        if is_postgres:
            conds = " AND ".join(
                f"LOWER(name) LIKE LOWER(:t{i})" for i in range(len(syn_tokens))
            )
            params = {
                f"t{i}": f"%{_escape_like(t)}%" for i, t in enumerate(syn_tokens)
            }
            params["lim"] = limit
            result = await session.execute(
                text(f"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE {obj_types} AND {conds}
                    ORDER BY LENGTH(name), uid
                    LIMIT :lim
                """),
                params,
            )
        else:
            fts_tokens = [_sanitize_fts_token(t) for t in syn_tokens]
            fts_tokens = [t for t in fts_tokens if t]
            if not fts_tokens:
                continue
            match_term = " ".join(fts_tokens)
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE {obj_types}
                      AND metadata_index_fts MATCH :mt
                    ORDER BY rank, LENGTH(name), uid
                    LIMIT :lim
                """),
                {"mt": match_term, "lim": limit},
            )
        rows = result.fetchall()
        if rows:
            return _build_indicator_results(rows, name, 0.80)

    # -------------------------------------------------------------------
    # Strategy 4: Any single meaningful token (broadest search)
    # -------------------------------------------------------------------
    meaningful = [t for t in (primary_tokens or name.split()) if len(t) >= 3]
    # Also add synonym expansions of individual tokens
    extra_tokens: list[str] = []
    for t in meaningful:
        syns = _HEALTH_SYNONYMS.get(t.lower())
        if syns:
            extra_tokens.extend(syns[:2])
    all_single_tokens = meaningful + extra_tokens

    for token in all_single_tokens:
        if is_postgres:
            pat = f"%{_escape_like(token)}%"
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE {obj_types}
                      AND (LOWER(name) LIKE LOWER(:pat) ESCAPE '\')
                    ORDER BY LENGTH(name), uid
                    LIMIT :lim
                """),
                {"pat": pat, "lim": limit},
            )
        else:
            san = _sanitize_fts_token(token)
            if not san:
                continue
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE {obj_types}
                      AND metadata_index_fts MATCH :mt
                    ORDER BY rank, LENGTH(name), uid
                    LIMIT :lim
                """),
                {"mt": san, "lim": limit},
            )
        rows = result.fetchall()
        if rows:
            return _build_indicator_results(rows, name, 0.72)

    return []


async def search_category_by_name(
    session,
    name: str,
    *,
    is_postgres: bool = False,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Search categories by name in the synced metadata index.

    Tries exact match first, then LIKE. Works with both Postgres and SQLite.
    """
    from sqlalchemy import text

    table = "metadata_index" if is_postgres else "metadata_index_fts"

    search_terms = [name]

    rows = []
    matched_exact = False
    for term in search_terms:
        # 1. Exact case-insensitive match
        result = await session.execute(
            text(f"""
                SELECT uid, name, object_type, short_name, description
                FROM {table}
                WHERE object_type = 'category'
                  AND LOWER(name) = LOWER(:name)
                ORDER BY LENGTH(name), uid
                LIMIT :lim
            """),
            {"name": term, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            matched_exact = True
            break

        # 2. LIKE pattern match / MATCH
        if is_postgres:
            pattern = f"%{_escape_like(term)}%"
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE object_type = 'category'
                      AND (LOWER(name) LIKE LOWER(:pattern) ESCAPE '\'
                           OR LOWER(short_name) LIKE LOWER(:pattern) ESCAPE '\')
                    ORDER BY LENGTH(name), uid
                    LIMIT :lim
                """),
                {"pattern": pattern, "lim": limit},
            )
        else:
            tokens = [_sanitize_fts_token(t) for t in term.split()]
            tokens = [t for t in tokens if t]
            match_term = " ".join(tokens) if tokens else f'"{term.replace(chr(34), "")}"'
            result = await session.execute(
                text(rf"""
                    SELECT uid, name, object_type, short_name, description
                    FROM {table}
                    WHERE object_type = 'category'
                      AND metadata_index_fts MATCH :match_term
                    ORDER BY rank, LENGTH(name), uid
                    LIMIT :lim
                """),
                {"match_term": match_term, "lim": limit},
            )
        rows = result.fetchall()
        if rows:
            break

    results = []
    for row in rows:
        exact_name = row[1].lower() == name.lower()
        if exact_name:
            confidence = 0.95
        elif matched_exact:
            confidence = 0.85
        else:
            confidence = 0.65
        results.append({
            "uid": row[0],
            "label": row[1],
            "object_type": row[2],
            "short_name": row[3],
            "description": row[4],
            "uid_confidence": confidence,
        })
    return results
