"""Database session management — lazy engine creation with init_db().

Supports both PostgreSQL (asyncpg) and SQLite (aiosqlite).
Engine is created lazily on first use, not at import time.

SQLite concurrency: uses WAL journal mode and check_same_thread=False
to prevent 'database is locked' errors under async concurrency.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import get_settings

logger = logging.getLogger("dhis2_analyst.db")

_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        is_sqlite = not settings.is_postgres
        logger.info(
            "db_engine_create",
            extra={
                "database_type": "sqlite" if is_sqlite else "postgres",
                "database_url_masked": settings.database_url.split("@")[-1] if "@" in settings.database_url else "(local)",
            },
        )

        engine_kwargs = {
            "pool_pre_ping": True,
            "echo": False,
        }

        if is_sqlite:
            # SQLite needs check_same_thread=False for async and constrained
            # pool to prevent concurrent write contention.
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            engine_kwargs["pool_size"] = 1
            engine_kwargs["max_overflow"] = 2

        _engine = create_async_engine(settings.database_url, **engine_kwargs)

        # Enable WAL mode on every new SQLite connection for concurrent reads.
        if is_sqlite:
            @event.listens_for(_engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                # SQLite ignores FK constraints unless enabled per connection —
                # without this, ON DELETE CASCADE is a no-op and deleting a
                # conversation orphans its (health-query) messages forever.
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            logger.info("db_sqlite_wal_enabled")

    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields an async session."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Context manager for use outside FastAPI dependency injection."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


async def init_db() -> None:
    """Initialise database schema. Creates tables if they don't exist."""
    from sqlalchemy import text
    settings = get_settings()

    logger.info("db_init_start", extra={"database_type": "postgres" if settings.is_postgres else "sqlite"})

    async with _get_engine().begin() as conn:
        if settings.is_postgres:
            # pgvector schema
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS metadata_index (
                    uid TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    short_name TEXT,
                    description TEXT,
                    dataset_names TEXT,
                    embedding vector(1536),
                    raw_metadata JSONB,
                    last_synced_at TIMESTAMPTZ
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS metadata_index_embedding_idx
                ON metadata_index USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """))
            # Conversation history. Timestamps are stored as ISO-8601 TEXT to
            # match the values the persistence layer binds (asyncpg rejects a
            # Python str bound to a TIMESTAMPTZ column). ISO-8601 sorts
            # lexicographically in chronological order, so ORDER BY still works.
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS conv_user_idx
                ON conversations(user_id, updated_at DESC)
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    artefacts JSONB,
                    created_at TEXT NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS msg_conv_idx
                ON messages(conversation_id, created_at)
            """))
            # Idempotent migration: earlier revisions declared these timestamp
            # columns TIMESTAMPTZ, but the persistence layer binds ISO strings
            # (asyncpg rejects str→timestamptz). Convert any pre-existing tables
            # to TEXT. ALTER ... TYPE TEXT is a no-op when already TEXT.
            for tbl, col in (
                ("conversations", "created_at"), ("conversations", "updated_at"),
                ("messages", "created_at"),
            ):
                try:
                    await conn.execute(text(
                        f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE TEXT "
                        f"USING {col}::text"
                    ))
                except Exception as exc:
                    logger.warning("db_timestamp_migration_skipped", extra={"table": tbl, "column": col, "error": str(exc)})
            logger.info("db_init_postgres_complete")
        else:
            # SQLite schema (no vector support)
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS metadata_index_lite (
                    uid TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    short_name TEXT,
                    description TEXT,
                    dataset_names TEXT,
                    raw_metadata TEXT,
                    last_synced_at TEXT
                )
            """))
            # Conversation history (SQLite)
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS conv_user_idx
                ON conversations(user_id, updated_at DESC)
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    artefacts TEXT,
                    created_at TEXT NOT NULL
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS msg_conv_idx
                ON messages(conversation_id, created_at)
            """))
            logger.info("db_init_sqlite_complete")
