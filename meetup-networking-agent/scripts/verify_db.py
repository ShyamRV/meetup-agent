"""Quick read-only sanity check that ``DATABASE_URL`` points at a
Postgres+pgvector install with the meetup schema applied.

Run::

    python scripts/verify_db.py

Exits 0 if the schema is present, 1 otherwise. Safe to use as a CI
gate after :mod:`scripts.bootstrap_db`.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


EXPECTED_TABLES = {
    "meetup_events",
    "meetup_attendees",
    "meetup_embeddings",
    "meetup_connections",
}


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(url)
    try:
        ext = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'meetup_%' "
            "ORDER BY table_name"
        )
        tables = [r["table_name"] for r in rows]
        counts = {}
        for t in tables:
            counts[t] = await conn.fetchval(f"SELECT count(*) FROM {t}")
    finally:
        await conn.close()

    print(f"pgvector version: {ext or 'NOT INSTALLED'}")
    print(f"meetup tables   : {tables}")
    print(f"row counts      : {counts}")

    missing = EXPECTED_TABLES - set(tables)
    if not ext:
        print("FAIL: pgvector extension is missing", file=sys.stderr)
        return 1
    if missing:
        print(f"FAIL: missing tables: {sorted(missing)}", file=sys.stderr)
        return 1
    print("OK: schema looks healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
