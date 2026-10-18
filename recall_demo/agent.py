"""ADK agents for the Recall demo apps. Their only long-term memory is a Recall memory server.

Each request runs with the signed-in user's own Auth0 token, held in a context
variable for that request only. The MCP toolset's header_provider reads it, so the
memory server sees that user and nobody else. The token is never stored in ADK
session state, logged or returned.
"""

from contextvars import ContextVar
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types

from .instructions import DemoApp

current_token: ContextVar[str | None] = ContextVar("recall_current_token", default=None)


class NoTokenForRequest(RuntimeError):
    """A memory tool was called outside an authenticated request."""


def bearer_header(_context: Any) -> dict[str, str]:
    token = current_token.get()
    if not token:
        raise NoTokenForRequest("no signed-in user for this request")
    return {"Authorization": f"Bearer {token}"}


def build_agent(app: DemoApp, memory_mcp_url: str, model: str, thinking_level: str | None = None) -> LlmAgent:
    config = None
    if thinking_level:
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level))
        )
    return LlmAgent(
        name=app.key,
        model=model,
        description=app.description,
        instruction=app.instruction,
        generate_content_config=config,
        tools=[
            McpToolset(
                connection_params=StreamableHTTPConnectionParams(url=memory_mcp_url, timeout=30.0),
                header_provider=bearer_header,
            )
        ],
    )
