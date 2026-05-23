"""Render a print-ready QR code that drops the user straight into the agent.

The QR encodes an ASI:One chat URL with the agent address and the
event UUID pre-filled, so attendees scan once and the agent receives
``event_id`` in the first chat turn.

Usage::

    python scripts/print_qr.py \
        --agent-address agent1q... \
        --event-id      c7458c01-908e-49c8-b21a-bfe358cf3681 \
        --event-name    "Innovation Lab Meetup" \
        --out           meetup_qr.png

Output is a PNG with the QR centred on a white background and the event
name printed underneath at a print-friendly size (A5 looks great).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlencode

try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - friendly error
    raise SystemExit(
        "missing dependency — run `uv add qrcode[pil]` or "
        "`pip install qrcode[pil]` and re-run"
    ) from exc


ASI_ONE_BASE = "https://asi1.ai/chat"


def build_chat_url(*, agent_address: str, event_id: str, intent: str | None) -> str:
    params: dict[str, str] = {"agent": agent_address, "event_id": event_id}
    if intent:
        params["intent"] = intent
    return f"{ASI_ONE_BASE}?{urlencode(params)}"


def render_qr(
    *,
    url: str,
    event_name: str,
    out_path: Path,
    box_size: int = 16,
    border: int = 2,
) -> None:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    qr_w, qr_h = qr_img.size
    padding = 80
    caption_h = 110 if event_name else 0
    canvas = Image.new(
        "RGB",
        (qr_w + 2 * padding, qr_h + 2 * padding + caption_h),
        "white",
    )
    canvas.paste(qr_img, (padding, padding))

    if event_name:
        draw = ImageDraw.Draw(canvas)
        font = _load_font(40)
        text_w = draw.textlength(event_name, font=font)
        draw.text(
            ((canvas.width - text_w) / 2, qr_h + padding + 30),
            event_name,
            fill="black",
            font=font,
        )

    canvas.save(out_path)


def _load_font(size: int) -> ImageFont.ImageFont:
    # Try a few common fonts; PIL's bitmap default is fine if none exist.
    for candidate in (
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-address", required=True, help="agent1q... address")
    parser.add_argument("--event-id", required=True, help="meetup event UUID")
    parser.add_argument("--event-name", default="", help="Caption under the QR")
    parser.add_argument("--intent", default=None, help="Optional preset intent")
    parser.add_argument(
        "--out", default="meetup_qr.png", help="Output PNG path"
    )
    parser.add_argument("--print-url", action="store_true")
    args = parser.parse_args(argv)

    url = build_chat_url(
        agent_address=args.agent_address,
        event_id=args.event_id,
        intent=args.intent,
    )
    out_path = Path(args.out)
    render_qr(url=url, event_name=args.event_name, out_path=out_path)

    print(f"QR saved to {out_path.resolve()}")
    if args.print_url:
        print(f"encoded URL: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
