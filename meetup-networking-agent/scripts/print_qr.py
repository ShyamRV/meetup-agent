#!/usr/bin/env python3
"""Render a print-ready QR for the meetup event.

The QR encodes an ASI:One chat URL with the agent address and the
event UUID pre-filled, so attendees scan once and the agent receives
``event_id`` in the first chat turn.

Usage::

    python scripts/print_qr.py --agent agent1q... --event-id <uuid>
    # or rely on env vars
    AGENT_ADDRESS=agent1q... DEFAULT_EVENT_ID=<uuid> python scripts/print_qr.py

Output is written to ``meetup_qr.png`` in the project root.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import qrcode
except ImportError as exc:  # pragma: no cover - friendly error
    raise SystemExit(
        "missing dependency — run `pip install qrcode[pil]` or "
        "`uv add qrcode[pil]` and re-run"
    ) from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=os.environ.get("AGENT_ADDRESS", ""))
    parser.add_argument(
        "--event-id", default=os.environ.get("DEFAULT_EVENT_ID", "")
    )
    parser.add_argument(
        "--intent",
        default="networking",
        help="Optional intent prefilled in the chat URL.",
    )
    parser.add_argument(
        "--out",
        default=str(PROJECT_ROOT / "meetup_qr.png"),
        help="Output PNG path (default: meetup_qr.png in project root).",
    )
    args = parser.parse_args()

    if not args.agent or not args.event_id:
        print(
            "ERROR: provide --agent and --event-id "
            "or set AGENT_ADDRESS and DEFAULT_EVENT_ID",
            file=sys.stderr,
        )
        return 1

    url = (
        "https://asi1.ai/chat"
        f"?agent={args.agent}"
        f"&event_id={args.event_id}"
        f"&intent={args.intent}"
    )

    print()
    print(f"QR URL:        {url}")
    print(f"Agent address: {args.agent}")
    print(f"Event ID:      {args.event_id}")
    print()

    img = qrcode.make(url)
    img.save(args.out)
    print(f"Saved: {args.out} \u2014 print this at A5 size")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
