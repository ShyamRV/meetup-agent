"""Apply every ``agents-db/migrations/*.sql`` file in order against ``DATABASE_URL``.

Designed to be run once after provisioning a fresh Postgres database
(Railway, Neon, Supabase, etc.) — it enables ``pgcrypto`` + ``vector``
extensions then creates the meetup tables. The migrations are safe to
re-run: every ``CREATE TABLE`` / ``CREATE INDEX`` uses ``IF NOT EXISTS``
or ``CREATE OR REPLACE`` patterns.

Usage::

    DATABASE_URL=postgresql://user:pass@host:port/db \
        python scripts/bootstrap_db.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

_CANDIDATE_DIRS = (
    # Local checkout layout: <repo>/agents-db/migrations
    Path(__file__).resolve().parents[2] / "agents-db" / "migrations",
    # Container layout: /app/agents-db/migrations
    Path("/app/agents-db/migrations"),
    # Optional override
    Path(os.environ.get("MIGRATIONS_DIR", "")),
)


def _find_migrations_dir() -> Path | None:
    for candidate in _CANDIDATE_DIRS:
        if candidate and candidate.is_dir():
            return candidate
    return None


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        return 1

    migrations_dir = _find_migrations_dir()
    if migrations_dir is None:
        searched = "\n  ".join(str(p) for p in _CANDIDATE_DIRS if str(p))
        print(
            "ERROR: migrations dir not found; searched:\n  " + searched,
            file=sys.stderr,
        )
        return 1

    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        print(f"ERROR: no .sql files in {migrations_dir}", file=sys.stderr)
        return 1
    print(f"-> using migrations from {migrations_dir}")

    conn = await asyncpg.connect(url)
    try:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            print(f"-> applying {path.name} ({len(sql)} bytes)")
            await conn.execute(sql)
        print(f"applied {len(files)} migration(s)")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
