"""Demo agent for `adk web`: its only long-term memory is the deployed memory-mcp server.

No ADK MemoryService and no session-state memory: a new ADK session starts with an
empty history, so anything the agent "remembers" came from the MCP tools.
"""

import os
import sys

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

from .auth import CachedTokenSource, DemoAuthError, DemoIdentity, header_provider
from .instructions import DESCRIPTION, INSTRUCTION

DEFAULT_MODEL = "gemini-3.5-flash"  # gemini-2.5-flash retires on Vertex AI on 2026-10-20


def _server_url() -> str:
    url = os.environ.get("MEMORY_MCP_URL", "").strip()
    if not url:
        raise RuntimeError(
            "MEMORY_MCP_URL is not set. Copy demo_agent/.env.example to demo_agent/.env and set it to "
            "the deployed server, e.g. https://memory-mcp-140715294513.us-central1.run.app/mcp"
        )
    if not url.rstrip("/").endswith("/mcp"):
        raise RuntimeError(f"MEMORY_MCP_URL must end with /mcp, got {url!r}")
    return url


identity = DemoIdentity.from_env()
token_source = CachedTokenSource(identity)

# Warn (don't crash) at load: you may sign in after starting adk web.
try:
    token_source.token()
except DemoAuthError as exc:
    print(f"WARNING: {exc}", file=sys.stderr)

memory_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(url=_server_url(), timeout=30.0),
    header_provider=header_provider(token_source),
)

root_agent = LlmAgent(
    name="memory_agent",
    model=os.environ.get("DEMO_AGENT_MODEL", "").strip() or DEFAULT_MODEL,
    description=DESCRIPTION,
    instruction=INSTRUCTION,
    tools=[memory_toolset],
)
