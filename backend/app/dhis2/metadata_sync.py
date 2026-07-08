"""Metadata sync job — pulls DHIS2 metadata and upserts into the metadata index.

Pulls from four DHIS2 endpoints:
- /api/dataElements
- /api/indicators
- /api/organisationUnits
- /api/programIndicators

Generates embeddings via LiteLLM when a real embedding provider is configured.
Falls back to keyword-only indexing when provider=mock.

Designed to run nightly via cron or on-demand via POST /api/metadata/sync.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.config import Settings

logger = logging.getLogger("dhis2_analyst.metadata_sync")

SYNC_ENDPOINTS = [
    ("dataElements", "dataElement", "id,name,shortName,description,dataSetElements[dataSet[name]]"),
    ("indicators", "indicator", "id,name,shortName,description"),
    ("organisationUnits", "orgUnit", "id,name,shortName,level"),
    ("programIndicators", "programIndicator", "id,name,shortName,description"),
    ("categories", "category", "id,name,shortName,description"),
]


async def sync_metadata(settings: Settings) -> dict[str, Any]:
    """Pull metadata from DHIS2 and upsert into the index."""
    import time
    start_time = time.time()

    if not settings.dhis2_service_account_user:
        logger.info("metadata_sync_skipped", extra={"reason": "No service account credentials"})
        return {
            "status": "skipped",
            "reason": "No DHIS2 service account credentials configured",
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "dhis2_base_url": settings.dhis2_base_url,
        }

    from backend.app.dhis2.client import DHIS2Client

    client = DHIS2Client(settings)
    totals = {"synced": 0, "skipped": 0, "failed": 0}
    endpoints_failed: list[str] = []

    logger.info("metadata_sync_started", extra={"dhis2_base_url": settings.dhis2_base_url})

    for endpoint, object_type, fields in SYNC_ENDPOINTS:
        endpoint_start = time.time()
        try:
            items = await client.metadata(endpoint, fields=fields)
            pull_duration = time.time() - endpoint_start
            logger.info(
                "metadata_pull_success",
                extra={
                    "endpoint": endpoint,
                    "count": len(items),
                    "pull_duration_seconds": round(pull_duration, 3),
                },
            )

            endpoint_synced = 0
            endpoint_failed = 0
            for item in items:
                try:
                    await _upsert_item(item, object_type, settings)
                    totals["synced"] += 1
                    endpoint_synced += 1
                except Exception as exc:
                    totals["failed"] += 1
                    endpoint_failed += 1
                    logger.warning(
                        "metadata_upsert_failed",
                        extra={"uid": item.get("id"), "error": str(exc)},
                    )

            endpoint_duration = time.time() - endpoint_start
            logger.info(
                "metadata_endpoint_complete",
                extra={
                    "endpoint": endpoint,
                    "synced": endpoint_synced,
                    "failed": endpoint_failed,
                    "total_duration_seconds": round(endpoint_duration, 3),
                },
            )

        except Exception as exc:
            logger.error(
                "metadata_endpoint_failed",
                extra={"endpoint": endpoint, "error": str(exc)},
            )
            endpoints_failed.append(endpoint)

    total_duration = time.time() - start_time

    # Distinguish a healthy sync from a misconfiguration (bad credentials, wrong
    # base URL, timeout). "complete" that indexed nothing previously hid a fully
    # broken sync and left every NL query dead-ending against an empty index.
    if endpoints_failed and len(endpoints_failed) == len(SYNC_ENDPOINTS):
        status = "failed"
    elif endpoints_failed:
        status = "partial"
    else:
        status = "complete"

    logger.info(
        "metadata_sync_complete",
        extra={
            **totals,
            "status": status,
            "endpoints_failed": endpoints_failed,
            "total_duration_seconds": round(total_duration, 3),
        },
    )
    return {
        "status": status,
        **totals,
        "endpoints_failed": endpoints_failed,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "dhis2_base_url": settings.dhis2_base_url,
    }


async def _upsert_item(item: dict, object_type: str, settings: Settings) -> None:
    """Upsert a single metadata item into the index."""
    uid = item.get("id", "")
    name = item.get("name", "")
    short_name = item.get("shortName", "")
    description = item.get("description", "")

    # Extract dataset names for richer embedding signal
    dataset_names = ""
    for dse in item.get("dataSetElements", []):
        ds = dse.get("dataSet", {})
        if ds.get("name"):
            dataset_names += f" {ds['name']}"

    # Build embedding text
    embed_text = f"{name} {short_name} {description} {dataset_names}".strip()

    # Generate embedding vector
    embedding: list[float] = []
    if settings.is_postgres and settings.embedding_provider != "mock":
        try:
            from backend.app.llm import embed
            embedding = await embed(embed_text, settings)
        except Exception as exc:
            logger.warning("embedding_failed", extra={"uid": uid, "error": str(exc)})

    if settings.is_postgres:
        await _upsert_postgres(uid, object_type, name, short_name, description, dataset_names, embedding, item)
    else:
        await _upsert_sqlite(uid, object_type, name, short_name, description, dataset_names, item)


async def _upsert_postgres(
    uid: str,
    object_type: str,
    name: str,
    short_name: str,
    description: str,
    dataset_names: str,
    embedding: list[float],
    raw: dict,
) -> None:
    """Upsert into Postgres with pgvector."""
    import json
    from sqlalchemy import text
    from backend.app.db.session import get_db_session

    async with get_db_session() as session:
        embed_str = str(embedding) if embedding else None
        await session.execute(
            text("""
            INSERT INTO metadata_index (uid, object_type, name, short_name, description, dataset_names, embedding, raw_metadata, last_synced_at)
            VALUES (:uid, :ot, :name, :sn, :desc, :dsn, CAST(:emb AS vector), CAST(:raw AS jsonb), NOW())
            ON CONFLICT (uid) DO UPDATE SET
                object_type = EXCLUDED.object_type,
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name,
                description = EXCLUDED.description,
                dataset_names = EXCLUDED.dataset_names,
                embedding = EXCLUDED.embedding,
                raw_metadata = EXCLUDED.raw_metadata,
                last_synced_at = NOW()
            """),
            {
                "uid": uid, "ot": object_type, "name": name, "sn": short_name,
                "desc": description, "dsn": dataset_names, "emb": embed_str,
                "raw": json.dumps(raw),
            },
        )
        await session.commit()


async def _upsert_sqlite(
    uid: str,
    object_type: str,
    name: str,
    short_name: str,
    description: str,
    dataset_names: str,
    raw: dict,
) -> None:
    """Upsert into SQLite keyword-only index (no embedding vector)."""
    import json
    from sqlalchemy import text
    from backend.app.db.session import get_db_session

    async with get_db_session() as session:
        # FTS5 does not support INSERT OR REPLACE on a custom column like uid.
        # So we delete first, then insert.
        await session.execute(
            text("DELETE FROM metadata_index_fts WHERE uid = :uid"),
            {"uid": uid}
        )
        await session.execute(
            text("""
            INSERT INTO metadata_index_fts
            (uid, object_type, name, short_name, description, dataset_names, raw_metadata, last_synced_at)
            VALUES (:uid, :ot, :name, :sn, :desc, :dsn, :raw, :ts)
            """),
            {
                "uid": uid, "ot": object_type, "name": name, "sn": short_name,
                "desc": description, "dsn": dataset_names,
                "raw": json.dumps(raw),
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
        await session.commit()
