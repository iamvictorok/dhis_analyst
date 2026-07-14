"""Diagnose: test upsert using the app's actual engine and session factory."""
import asyncio
import os
import sys

# Set up the path so we can import the backend
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

async def main():
    from backend.app.db.session import init_db, get_db_session, _get_engine
    from sqlalchemy import text
    import json

    print("Initializing DB (same as app startup)...")
    await init_db()
    print("init_db complete")

    embedding = [0.01] * 1536
    embed_str = str(embedding)

    # Try upsert using the app's own get_db_session
    for i in range(3):
        uid = f"TEST_APP_{i:03d}"
        try:
            async with get_db_session() as session:
                await session.execute(
                    text("""
                    INSERT INTO metadata_index (uid, object_type, name, short_name, description, dataset_names, embedding, raw_metadata, last_synced_at)
                    VALUES (:uid, :ot, :name, :sn, :desc, :dsn, CAST(:emb AS vector), CAST(:raw AS jsonb), NOW())
                    ON CONFLICT (uid) DO UPDATE SET
                        name = EXCLUDED.name,
                        embedding = EXCLUDED.embedding,
                        last_synced_at = NOW()
                    """),
                    {
                        "uid": uid, "ot": "dataElement", "name": f"Test {i}",
                        "sn": "Tst", "desc": "debug", "dsn": "",
                        "emb": embed_str, "raw": json.dumps({"id": uid}),
                    },
                )
                await session.commit()
                print(f"  [{i}] SUCCESS")
        except Exception as exc:
            print(f"  [{i}] FAILED — {type(exc).__name__}: {exc}")

    # Cleanup
    async with get_db_session() as session:
        await session.execute(text("DELETE FROM metadata_index WHERE uid LIKE 'TEST_APP_%'"))
        await session.commit()
        print("Cleaned up test rows")

    await _get_engine().dispose()

asyncio.run(main())
