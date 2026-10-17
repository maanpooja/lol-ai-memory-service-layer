"""Delete ALL memories of the demo identity, through the deployed MCP server.

Scoped by construction: it uses the demo user's own cached token, and the
server only ever lets a token see and delete its own user's memories. You must
also name the user on the command line, and it must match DEMO_USER_ID.

list_memories reads the search index, which lags writes by about a minute.
Memories added in the last minute may be missed: wait ~1 minute after a demo
before resetting, and ~1 minute after resetting before the next demo.

Usage:
    uv run --group agent python scripts/reset_demo_user.py --user alice
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "demo_agent")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "demo_agent" / ".env")

import httpx  # noqa: E402

from memory_agent.auth import CachedTokenSource, DemoAuthError, DemoIdentity, auth_headers, token_path  # noqa: E402

MAX_ROUNDS = 3


class ToolFailed(Exception):
    pass


def call_tool(url: str, headers: dict[str, str], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One stateless MCP tools/call over HTTP. Returns structuredContent; raises ToolFailed on a tool error."""
    response = httpx.post(
        url,
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        timeout=90,
    )
    if response.status_code != 200:
        raise ToolFailed(f"HTTP {response.status_code}")
    data = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
    result = data.get("result") or {}
    if "error" in data or result.get("isError"):
        message = (result.get("content") or [{}])[0].get("text") or data.get("error", {}).get("message", "")
        raise ToolFailed(message)
    return result.get("structuredContent") or {}


def reset(identity: DemoIdentity, url: str) -> int:
    """Delete every memory the identity's token can see. Returns how many were deleted."""
    source = CachedTokenSource(identity)
    deleted = 0
    for _ in range(MAX_ROUNDS):
        memories = call_tool(url, auth_headers(source), "list_memories", {"limit": 100})["memories"]
        if not memories:
            break
        for memory in memories:
            try:
                if call_tool(url, auth_headers(source), "delete_memory", {"memory_id": memory["id"]}).get("deleted"):
                    deleted += 1
            except ToolFailed as exc:
                if "Memory not found" not in str(exc):  # already deleted, still in the index
                    raise
    return deleted


def main() -> int:
    identity = DemoIdentity.from_env()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", required=True, help="must equal DEMO_USER_ID, as a guard")
    args = parser.parse_args()
    if args.user != identity.user_id:
        print(f"REFUSED: --user {args.user!r} does not match DEMO_USER_ID={identity.user_id!r}.", file=sys.stderr)
        return 2

    url = os.environ.get("MEMORY_MCP_URL", "").strip()
    if not url:
        print("ERROR: set MEMORY_MCP_URL in demo_agent/.env", file=sys.stderr)
        return 2
    try:
        email = json.loads(token_path(identity).read_text()).get("email")
    except (OSError, ValueError):
        email = None
    print(f"Target: tenant={identity.tenant_id} user={identity.user_id} ({email or 'not signed in'}) at {url}")
    try:
        deleted = reset(identity, url)
    except DemoAuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ToolFailed as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Deleted {deleted} memories. Wait ~1 minute for search to settle before the next demo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
