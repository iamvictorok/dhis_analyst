"""Metadata index — schema definition, upsert, and search functions.

Supports:
- pgvector cosine similarity search (Postgres)
- Keyword-based search (SQLite fallback)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("dhis2_analyst.metadata_index")


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so user/LLM text is matched literally.

    Without this, a ``%`` in the term matches every row and ``_`` matches any
    character, which — combined with LIMIT — returns an arbitrary row as a
    "confident" match. Paired with ``ESCAPE '\'`` in the SQL.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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

    pattern = f"%{_escape_like(term)}%"
    result = await session.execute(
        text(r"""
            SELECT uid, name, object_type, short_name, description
            FROM metadata_index_lite
            WHERE name LIKE :pattern ESCAPE '\'
               OR short_name LIKE :pattern ESCAPE '\'
               OR description LIKE :pattern ESCAPE '\'
               OR dataset_names LIKE :pattern ESCAPE '\'
            ORDER BY
                CASE WHEN LOWER(name) = LOWER(:term) THEN 0 ELSE 1 END,
                LENGTH(name),
                uid
            LIMIT :lim
        """),
        {"pattern": pattern, "term": term, "lim": limit},
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
    Works with both PostgreSQL (metadata_index) and SQLite (metadata_index_lite).
    """
    import re
    import json
    from sqlalchemy import text

    table = "metadata_index" if is_postgres else "metadata_index_lite"

    def clean_name(n: str) -> str:
        # Strip common qualifiers
        cleaned = re.sub(
            r"\b(district|lga|ward|facility|state|county|province|country|clinic|hospital|chc|chp|mchp|health\s+center|health\s+post)\b",
            "",
            n,
            flags=re.IGNORECASE
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

        # 2. Fall back to LIKE pattern match (shortest/most-specific name first)
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
        rows = result.fetchall()
        if rows:
            break

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
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Search indicators/dataElements by name in the synced metadata index.

    Tries exact match first, then LIKE. Works with both Postgres and SQLite.
    """
    import re
    from sqlalchemy import text

    table = "metadata_index" if is_postgres else "metadata_index_lite"

    # Clean common qualifiers that might mismatch
    def clean_label(n: str) -> str:
        cleaned = re.sub(
            r"\b(coverage|rate|total|cases|confirmed|suspected)\b",
            "",
            n,
            flags=re.IGNORECASE,
        )
        return cleaned.strip()

    search_terms = [name]
    cleaned = clean_label(name)
    if cleaned and cleaned.lower() != name.lower():
        search_terms.append(cleaned)

    rows = []
    matched_exact = False
    for term in search_terms:
        # 1. Exact case-insensitive match
        result = await session.execute(
            text(f"""
                SELECT uid, name, object_type, short_name, description
                FROM {table}
                WHERE object_type IN ('indicator', 'dataElement', 'programIndicator')
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

        # 2. LIKE pattern match — shortest (most specific) name first, with uid
        # as a stable tiebreaker so the same query resolves the same way across
        # syncs (previously order was unspecified → nondeterministic UID).
        pattern = f"%{_escape_like(term)}%"
        result = await session.execute(
            text(rf"""
                SELECT uid, name, object_type, short_name, description
                FROM {table}
                WHERE object_type IN ('indicator', 'dataElement', 'programIndicator')
                  AND (LOWER(name) LIKE LOWER(:pattern) ESCAPE '\'
                       OR LOWER(short_name) LIKE LOWER(:pattern) ESCAPE '\')
                ORDER BY LENGTH(name), uid
                LIMIT :lim
            """),
            {"pattern": pattern, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            break

    results = []
    for row in rows:
        exact_name = row[1].lower() == name.lower()
        # Graded confidence: an exact name match clears the disambiguation gate;
        # a bare substring match stays below it so weak guesses surface
        # candidates instead of being silently charted as the answer.
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
