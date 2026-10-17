"""Show the agent remembering a user across two separate sessions.

Superseded by demo_agent/ (run with `adk web demo_agent`, see demo_agent/README.md).
Kept for reference.

Session 1: the user introduces themselves; the agent saves facts with add_memory.
Wait for the search index (about a minute).
Session 2: a brand-new ADK session (new session service, no shared history)
asks for a recommendation; the agent finds the facts with search_memory.
Optionally, a second user asks the same question and gets no one else's facts.

Usage (from the memory-mcp folder):
    uv run --group agent python agent/demo.py                          # device sign-in
    uv run --group agent python agent/demo.py --username alice@demo.test --other bob@demo.test
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from auth0_login import login  # noqa: E402
from memory_agent.agent import TOKENS, memory_tools, root_agent  # noqa: E402

APP = "memory-demo"
SESSION_1 = [
    "Hi! I'm a backend engineer based in Pune. I'm vegetarian and I mostly work on a ThinkPad X1.",
    "Also, I'm leading the payments reconciliation project this quarter.",
]
SESSION_2 = ["Can you suggest a good place for lunch near me, and what should I order?"]


async def run_session(user: str, messages: list[str], label: str) -> None:
    """One ADK session with its own fresh session service: nothing carries over except the MCP server."""
    runner = Runner(app_name=APP, agent=root_agent, session_service=InMemorySessionService())
    session = await runner.session_service.create_session(app_name=APP, user_id=user)
    print(f"\n=== {label} (user={user}, session={session.id[:8]}) ===")
    for text in messages:
        print(f"\nuser> {text}")
        started = time.monotonic()
        async for event in runner.run_async(
            user_id=user, session_id=session.id, new_message=types.Content(role="user", parts=[types.Part(text=text)])
        ):
            for call in event.get_function_calls():
                print(f"  [tool] {call.name}")
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(part.text or "" for part in event.content.parts)
                print(f"agent> {reply.strip()}")
        print(f"  ({time.monotonic() - started:.1f}s)")


class _Ctx:
    """Just enough of a ReadonlyContext for the bearer header provider."""

    state: dict = {}

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


async def check_server(user: str) -> bool:
    """Fail fast with one clear line if the memory server is unreachable or rejects the token."""
    try:
        tools = await memory_tools.get_tools(_Ctx(user))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot use the memory server at {os.environ.get('MEMORY_MCP_URL')}: {exc}")
        print("Check MEMORY_MCP_URL in agent/.env (must end in /mcp) and that you signed in.")
        return False
    print(f"connected: {len(tools)} memory tools ({', '.join(sorted(t.name for t in tools))})")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", help="password-grant demo user (default: device sign-in)")
    parser.add_argument("--other", help="a second demo user who must not see the first user's memories")
    parser.add_argument("--wait", type=int, default=75, help="seconds to wait for the search index")
    args = parser.parse_args()

    print(f"memory server: {os.environ.get('MEMORY_MCP_URL')}")
    TOKENS["user-1"] = login(args.username)
    if not await check_server("user-1"):
        return 1
    await run_session("user-1", SESSION_1, "Session 1: getting to know the user")

    print(f"\nwaiting {args.wait}s for the search index to pick up new memories ...", flush=True)
    await asyncio.sleep(args.wait)
    await run_session("user-1", SESSION_2, "Session 2: new session, same user")

    if args.other:
        TOKENS["user-2"] = login(args.other)
        await run_session("user-2", SESSION_2, "Session 3: a different user")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
