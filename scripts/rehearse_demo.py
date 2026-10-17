"""Headless rehearsal of the live adk web demo, with PASS/FAIL checks.

1. Reset the demo user's memories, then wait for the index to settle.
2. Session 1: the user introduces themselves -> the agent must call add_memory.
3. Wait for indexing (visible countdown).
4. Session 2, a NEW session (fresh session service, no shared history):
   "Suggest a good dinner spot and a laptop bag for me." -> the agent must read memory
   before its first reply, and the reply must mention Pune, vegetarian or ThinkPad.

Prints memory facts to the console. Only use it with the synthetic demo users,
never with real data.

Usage:
    uv run --group agent python scripts/rehearse_demo.py
    uv run --group agent python scripts/rehearse_demo.py --wait 75 --settle 60
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "demo_agent"), str(ROOT / "scripts")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "demo_agent" / ".env")

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from memory_agent.agent import identity, root_agent  # noqa: E402
from reset_demo_user import reset  # noqa: E402

APP = "memory-demo-rehearsal"
SESSION_1 = "Hi! I'm a backend engineer based in Pune. I'm vegetarian and I mostly work on a ThinkPad X1."
SESSION_2 = "Suggest a good dinner spot and a laptop bag for me."
PERSONAL = ("pune", "vegetarian", "thinkpad")
MEMORY_READS = {"list_memories", "search_memory"}


@dataclass
class Turn:
    steps: list[tuple[str, str]] = field(default_factory=list)  # ("tool", name) or ("text", reply)
    reply: str = ""


def short(value: Any, limit: int = 90) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 3] + "..."


def outcome(response: dict[str, Any]) -> str:
    if response.get("isError") or response.get("is_error"):
        return "ERROR " + short((response.get("content") or [{}])[0].get("text", ""))
    data = response.get("structuredContent") or response.get("structured_content") or response
    parts = []
    for key in ("saved", "memories"):
        if isinstance(data.get(key), list):
            parts.append(f"{key}={len(data[key])}")
    for key in ("skipped_duplicates", "sensitive_not_saved", "rejected", "count", "selection", "deleted"):
        if key in data:
            parts.append(f"{key}={data[key]}")
    facts = [m.get("fact") for m in data.get("saved") or data.get("memories") or [] if isinstance(m, dict)]
    return " ".join(parts) + (f"  facts={facts}" if facts else "")


async def run_session(label: str, message: str) -> Turn:
    runner = Runner(app_name=APP, agent=root_agent, session_service=InMemorySessionService())
    session = await runner.session_service.create_session(app_name=APP, user_id="rehearsal")
    print(f"\n=== {label} (new session {session.id[:8]}) ===\nuser> {message}")
    turn = Turn()
    started = time.monotonic()
    async for event in runner.run_async(
        user_id="rehearsal", session_id=session.id, new_message=types.Content(role="user", parts=[types.Part(text=message)])
    ):
        for call in event.get_function_calls():
            turn.steps.append(("tool", call.name))
            print(f"  [call]   {call.name} {short(call.args or {})}")
        for response in event.get_function_responses():
            print(f"  [result] {response.name}: {outcome(response.response or {})}")
        if event.content and event.content.parts and not event.get_function_calls() and event.author != "user":
            text = "".join(part.text or "" for part in event.content.parts if not getattr(part, "thought", False))
            if text.strip():
                turn.steps.append(("text", text))
                turn.reply += text
    print(f"agent> {turn.reply.strip()}\n  ({time.monotonic() - started:.1f}s)")
    return turn


async def countdown(seconds: int, why: str) -> None:
    for left in range(seconds, 0, -1):
        print(f"\r{why}: {left:3d}s ", end="", flush=True)
        await asyncio.sleep(1)
    print(f"\r{why}: done   ")


def check(name: str, ok: bool, results: list[tuple[str, bool]]) -> None:
    results.append((name, ok))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wait", type=int, default=60, help="seconds between session 1 and 2 (indexing)")
    parser.add_argument("--settle", type=int, default=60, help="seconds to wait after the reset")
    parser.add_argument("--skip-reset", action="store_true")
    args = parser.parse_args()

    print(f"Demo identity: tenant={identity.tenant_id} user={identity.user_id}  server={os.environ['MEMORY_MCP_URL']}")
    if not args.skip_reset:
        print(f"Reset: deleted {reset(identity, os.environ['MEMORY_MCP_URL'])} memories")
        await countdown(args.settle, "waiting for the index to settle after reset")

    first = await run_session("Session 1", SESSION_1)
    await countdown(args.wait, "waiting for indexing before the new session")
    second = await run_session("Session 2", SESSION_2)

    results: list[tuple[str, bool]] = []
    check("session 1 called add_memory", ("tool", "add_memory") in first.steps, results)
    first_text = next((i for i, (kind, _) in enumerate(second.steps) if kind == "text"), len(second.steps))
    read_first = any(kind == "tool" and name in MEMORY_READS for kind, name in second.steps[:first_text])
    check("session 2 read memory before its first reply", read_first, results)
    check(
        "session 2 reply mentions Pune / vegetarian / ThinkPad",
        any(word in second.reply.lower() for word in PERSONAL),
        results,
    )

    print("\n" + "=" * 64)
    for name, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("=" * 64)
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
