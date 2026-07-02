"""Metadata index — schema definition, upsert, and search functions.

Supports:
- pgvector cosine similarity search (Postgres)
- Keyword-based search (SQLite fallback)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("dhis2_analyst.metadata_index")


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

    pattern = f"%{term}%"
    result = await session.execute(
        text("""
            SELECT uid, name, object_type, short_name, description
            FROM metadata_index_lite
            WHERE name LIKE :pattern
               OR short_name LIKE :pattern
               OR description LIKE :pattern
               OR dataset_names LIKE :pattern
            LIMIT :lim
        """),
        {"pattern": pattern, "lim": limit},
    )
    return [
        {
            "uid": row[0],
            "label": row[1],
            "object_type": row[2],
            "short_name": row[3],
            "description": row[4],
            "uid_confidence": 0.75,  # Fixed confidence for keyword matches
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
                LIMIT :lim
            """),
            {"name": term, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            break

        # 2. Fall back to LIKE pattern match
        pattern = f"%{term}%"
        result = await session.execute(
            text(f"""
                SELECT uid, name, short_name, raw_metadata
                FROM {table}
                WHERE object_type = 'orgUnit'
                  AND (LOWER(name) LIKE LOWER(:pattern)
                       OR LOWER(short_name) LIKE LOWER(:pattern))
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
    for term in search_terms:
        # 1. Exact case-insensitive match
        result = await session.execute(
            text(f"""
                SELECT uid, name, object_type, short_name, description
                FROM {table}
                WHERE object_type IN ('indicator', 'dataElement', 'programIndicator')
                  AND LOWER(name) = LOWER(:name)
                LIMIT :lim
            """),
            {"name": term, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            break

        # 2. LIKE pattern match
        pattern = f"%{term}%"
        result = await session.execute(
            text(f"""
                SELECT uid, name, object_type, short_name, description
                FROM {table}
                WHERE object_type IN ('indicator', 'dataElement', 'programIndicator')
                  AND (LOWER(name) LIKE LOWER(:pattern)
                       OR LOWER(short_name) LIKE LOWER(:pattern))
                LIMIT :lim
            """),
            {"pattern": pattern, "lim": limit},
        )
        rows = result.fetchall()
        if rows:
            break

    return [
        {
            "uid": row[0],
            "label": row[1],
            "object_type": row[2],
            "short_name": row[3],
            "description": row[4],
            "uid_confidence": 0.85,
        }
        for row in rows
    ]
