"""Record the DHIS2 demo snapshot used when every live instance is down.

Runs a full metadata sync plus each suggested demo question through the real
agent pipeline against the LIVE primary instance, capturing every DHIS2
response into backend/demo_snapshot/dhis2_snapshot.json (merged with whatever
is already there, so recording with several LLMs widens coverage).

Usage (uses your .env / environment for DHIS2 + LLM settings):

    python scripts/record_demo_snapshot.py               # all demo questions
    python scripts/record_demo_snapshot.py --metadata-only

Re-record before a demo if the snapshot date is old; commit the JSON file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / "backend" / "demo_snapshot" / "dhis2_snapshot.json"
QUESTIONS = ROOT / "backend" / "demo_snapshot" / "demo_questions.json"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SNAPSHOT))
    ap.add_argument("--metadata-only", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated demo question ids")
    args = ap.parse_args()

    # Record from the primary instance only, never from the old snapshot, and
    # into a throwaway DB so a fresh sync captures every metadata page.
    os.environ["DHIS2_SNAPSHOT_RECORD_PATH"] = args.out
    os.environ["DHIS2_SNAPSHOT_MODE"] = "off"
    os.environ["DHIS2_BASE_URLS"] = ""
    tmpdb = Path(tempfile.mkdtemp()) / "record.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmpdb}"

    from backend.config import get_settings
    from backend.app.db.session import init_db
    from backend.app.dhis2.metadata_sync import sync_metadata
    from backend.app.agent.graph import run_agent_stream
    from backend.app.models import ChatRequest, Identity

    settings = get_settings()
    if not settings.dhis2_service_account_user:
        print("DHIS2_SERVICE_ACCOUNT_USER/PASS must be set", file=sys.stderr)
        return 2
    await init_db()
    res = await sync_metadata(settings)
    print("metadata sync:", res["status"], res.get("synced"))
    if res["status"] != "complete":
        print("metadata sync not complete; snapshot may be partial", file=sys.stderr)
    if args.metadata_only:
        return 0

    only = {x for x in args.only.split(",") if x}
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    ident = Identity(user_id="snapshot-recorder")
    failures = 0
    for q in questions:
        if only and q["id"] not in only:
            continue
        events: list[str] = []
        async for chunk in run_agent_stream(
            ChatRequest(message=q["question"], output_mode=q.get("output_mode")), settings, ident
        ):
            line = chunk.split("\n", 1)[0]
            if line.startswith("event:"):
                events.append(line[6:].strip())
        ok = "error" not in events and any(
            e in events for e in ("chart_config", "report_html", "data_ready", "token")
        )
        failures += 0 if ok else 1
        print(f"{q['id']}: {'ok' if ok else 'FAILED'} {sorted(set(events))}")

    # Widen coverage: for every recorded analytics request also record its
    # national <-> per-district twin, so a model that picks the other scope
    # still finds data when the snapshot is serving.
    from urllib.parse import parse_qsl
    from backend.app.dhis2 import failover
    from backend.app.dhis2.client import DHIS2Client

    client = DHIS2Client(settings)
    me = await client.get("/api/me.json", params={"fields": "organisationUnits[id]"})
    roots = [o["id"] for o in me.get("organisationUnits", []) if o.get("id")]
    recorder = failover.get_recorder(settings)
    # The user profile is not needed offline; keep it out of the committed file.
    recorder.data["entries"].pop(
        failover.canonical_key("/api/me.json", {"fields": "organisationUnits[id]"}), None
    )
    if len(roots) == 1:
        recorder.set_meta(ou_aliases={"USER_ORGUNIT": roots[0]})
    for key in list(recorder.data["entries"]):
        if not key.startswith("/api/analytics"):
            continue
        params: dict = {}
        for k, v in parse_qsl(key.partition("?")[2]):
            params.setdefault(k, []).append(v)
        dims = []
        for d in params.get("dimension", []):
            if d.startswith("ou:"):
                items = [i for i in d[3:].split(";") if i]
                if any(i.startswith("LEVEL-") for i in items):
                    items = [i for i in items if not i.startswith("LEVEL-")]
                else:
                    items = items + ["LEVEL-2"]
                d = "ou:" + ";".join(items)
            dims.append(d)
        twin = {k: (v if k == "dimension" else v[0]) for k, v in params.items()}
        twin["dimension"] = dims
        try:
            await client.analytics(twin)
        except Exception as exc:
            print("twin failed:", dims, exc)

    data = json.loads(Path(args.out).read_text(encoding="utf-8"))
    print(f"snapshot: {len(data['entries'])} responses, recorded_at={data['meta'].get('recorded_at')} -> {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
