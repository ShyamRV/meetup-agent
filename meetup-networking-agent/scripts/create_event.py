#!/usr/bin/env python3
"""Create (or update) the meetup event row in Postgres.

Run once before the meetup. Idempotent on ``qr_seed`` — re-running with
the same ``--seed`` refreshes the name/location and returns the same
event UUID, so it's safe to use as part of a redeploy script.

Usage::

    DATABASE_URL=postgresql://... \
        python scripts/create_event.py \
            --name "Innovation Lab Meetup" \
            --location "Bangalore" \
            --seed EVT_MAY2026_001
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Innovation Lab Meetup")
    parser.add_argument("--location", default="Bangalore")
    parser.add_argument("--seed", default="EVT_MAY2026_001")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(url)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO meetup_events (event_name, qr_seed, event_date, location)
            VALUES ($1, $2, NOW(), $3)
            ON CONFLICT (qr_seed) DO UPDATE SET
                event_name = EXCLUDED.event_name,
                location   = EXCLUDED.location
            RETURNING id, event_name, location
            """,
            args.name,
            args.seed,
            args.location,
        )
    finally:
        await conn.close()

    bar = "=" * 50
    print()
    print(bar)
    print("EVENT CREATED")
    print(f"Event ID:   {row['id']}")
    print(f"Event Name: {row['event_name']}")
    print(f"Location:   {row['location']}")
    print(bar)
    print()
    print("Set this in your env:")
    print(f"DEFAULT_EVENT_ID={row['id']}")
    print()
    print(
        "Now run:\n"
        f"  python scripts/print_qr.py --agent <AGENT_ADDRESS> --event-id {row['id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
