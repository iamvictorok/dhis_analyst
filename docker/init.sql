-- pgvector schema init for DHIS2 AI Analyst metadata index.
-- Runs automatically on first Postgres container startup.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS metadata_index (
    uid TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT,
    description TEXT,
    dataset_names TEXT,
    embedding vector(3072),
    raw_metadata JSONB,
    last_synced_at TIMESTAMPTZ
);

-- Note: ivfflat/hnsw indices in pgvector support a maximum of 2000 dimensions.
-- Since we support 3072 dimensions (e.g. Gemini), we rely on exact search without an index.

CREATE INDEX IF NOT EXISTS metadata_index_name_idx
ON metadata_index USING gin (to_tsvector('english', name || ' ' || COALESCE(short_name, '') || ' ' || COALESCE(description, '')));
