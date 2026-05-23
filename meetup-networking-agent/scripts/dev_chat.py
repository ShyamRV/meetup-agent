"""Send one local ASI/uAgents chat message to the meetup agent.

This is a development-only test client. It creates a temporary uAgent,
sends a real ``ChatMessage`` to the target meetup agent, prints any chat
response, then exits.

Examples:

    # If you know the target address:
    python scripts/dev_chat.py \
        --target agent1... \
        --event-id <uuid>

    # Or derive it from the agent's seed (must match what agent.py runs with):
    python scripts/dev_chat.py \
        --target-seed "meetup-networking-agent-dev-seed" \
        --event-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import UTC, datetime

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.identity import Identity


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _chat_message(text: str, *, start: bool = False) -> ChatMessage:
    content = []
    if start:
        content.append(StartSessionContent(type="start-session"))
    content.append(TextContent(type="text", text=text))
    return ChatMessage(timestamp=_now(), msg_id=uuid.uuid4(), content=content)


def _resolve_target(args: argparse.Namespace) -> str:
    if args.target:
        return args.target
    if args.target_seed:
        identity = Identity.from_seed(args.target_seed, 0)
        return identity.address
    env_seed = os.getenv("AGENT_SEED")
    if env_seed:
        return Identity.from_seed(env_seed, 0).address
    raise SystemExit(
        "must supply --target, --target-seed, or set AGENT_SEED in env"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="Target meetup agent address")
    parser.add_argument(
        "--target-seed",
        help="Derive target address from this seed (must match the agent's AGENT_SEED)",
    )
    parser.add_argument("--event-id", required=True, help="Meetup event UUID")
    parser.add_argument(
        "--message",
        default="hello, I scanned the QR code",
        help="Message text to send after event_id",
    )
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()
    target_address = _resolve_target(args)

    # uAgents 0.25.x expects a loop at construction time on newer Python.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    client = Agent(
        name="meetup-local-test-client",
        seed=f"meetup-local-test-client-{uuid.uuid4()}",
        port=18001,
        endpoint=["http://localhost:18001/submit"],
    )
    chat_proto = Protocol(spec=chat_protocol_spec)
    background_tasks: set[asyncio.Task[None]] = set()

    async def _exit_later() -> None:
        await asyncio.sleep(args.timeout)
        print(f"[dev_chat] {args.timeout}s elapsed, exiting")
        os._exit(0)

    @client.on_event("startup")
    async def _startup(ctx: Context) -> None:
        task = asyncio.create_task(_exit_later())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        msg = _chat_message(
            f"{args.message}\nevent_id={args.event_id}",
            start=True,
        )
        print(f"[dev_chat] sending ChatMessage to {target_address}")
        await ctx.send(target_address, msg)

    @chat_proto.on_message(ChatAcknowledgement)
    async def _ack(_ctx: Context, _sender: str, msg: ChatAcknowledgement) -> None:
        print(f"[dev_chat] ack for {msg.acknowledged_msg_id}")

    @chat_proto.on_message(ChatMessage)
    async def _message(ctx: Context, sender: str, msg: ChatMessage) -> None:
        print(f"[dev_chat] response from {sender}:")
        for item in msg.content:
            if isinstance(item, TextContent):
                print(item.text)
                print("---")
        await ctx.send(
            sender,
            ChatAcknowledgement(
                timestamp=_now(),
                acknowledged_msg_id=msg.msg_id,
            ),
        )
        # The agent may send several ChatMessages back to back (a
        # welcome message followed by match results). We don't know
        # how many to expect, so just keep listening until the
        # timeout configured by ``_exit_later`` fires.

    client.include(chat_proto, publish_manifest=True)
    client.run()


if __name__ == "__main__":
    main()
