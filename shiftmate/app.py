"""ShiftMate web app: a chat page plus /api/chat, for judges to use in a browser.

- The page signs the user in with Auth0 (Universal Login) and sends the access token.
- /api/chat verifies that token with the same OIDC verifier as the memory server,
  then runs the agent with the token, so memory calls act as that user only.
- Never logs message text, replies or tokens. Errors returned to the browser are generic.
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from memory_mcp.oidc import OidcSettings, OidcTokenVerifier, stable_id

logger = logging.getLogger("shiftmate")
STATIC = Path(__file__).resolve().parent / "static"
APP_NAME = "shiftmate"
MAX_MESSAGE_CHARS = 2000


@dataclass(frozen=True)
class Settings:
    memory_mcp_url: str
    issuer: str
    audience: str
    auth0_domain: str
    auth0_client_id: str
    model: str = "gemini-3.5-flash"
    thinking_level: str | None = "MINIMAL"
    rate_limit: int = 30  # messages per user per window
    rate_window_seconds: int = 600

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise RuntimeError(f"shiftmate needs {name}")
            return value

        return cls(
            memory_mcp_url=required("MEMORY_MCP_URL"),
            issuer=required("OIDC_ISSUER"),
            audience=required("OIDC_AUDIENCE"),
            auth0_domain=required("AUTH0_DOMAIN"),
            auth0_client_id=required("AUTH0_SPA_CLIENT_ID"),
            model=os.environ.get("SHIFTMATE_MODEL", "").strip() or "gemini-3.5-flash",
            thinking_level=os.environ.get("SHIFTMATE_THINKING_LEVEL", "MINIMAL").strip().upper() or None,
        )


@dataclass
class Activity:
    tool: str
    summary: str


@dataclass
class TurnResult:
    session_id: str
    reply: str
    activity: list[Activity] = field(default_factory=list)


class ChatEngine(Protocol):
    async def run(self, user_key: str, session_id: str | None, message: str, token: str) -> TurnResult: ...


def summarize(tool: str, response: dict[str, Any]) -> str:
    """One line for the Memory activity panel. Shows the user's own facts, never other content."""
    if response.get("isError") or response.get("is_error"):
        text = (response.get("content") or [{}])[0].get("text", "")
        return "error: " + text.replace("Error executing tool ", "")[:120]
    data = response.get("structuredContent") or response.get("structured_content") or {}
    if tool == "add_memory":
        saved = data.get("saved") or []
        parts = [f"{s.get('op', '').lower()}: {s.get('fact')}" for s in saved]
        if data.get("skipped_duplicates"):
            parts.append(f"{data['skipped_duplicates']} already known")
        if data.get("sensitive_not_saved"):
            parts.append(f"{data['sensitive_not_saved']} not saved (privacy)")
        if data.get("rejected"):
            parts.append(f"{data['rejected']} rejected (e.g. authority claim)")
        return "; ".join(parts) or "nothing to save"
    if tool in ("list_memories", "search_memory"):
        facts = [m.get("fact") for m in data.get("memories") or []]
        return f"{len(facts)} found" + (": " + "; ".join(facts) if facts else "")
    if tool == "delete_memory":
        return "deleted" if data.get("deleted") else "not deleted"
    if tool == "update_memory":
        return f"updated: {data.get('fact')}"
    return "done"


class AdkChatEngine:
    """Runs the ADK agent. Sessions are in memory on this instance (deploy with max 1 instance)."""

    def __init__(self, settings: Settings) -> None:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        from .agent import build_agent

        self._runner = Runner(
            app_name=APP_NAME,
            agent=build_agent(settings.memory_mcp_url, settings.model, settings.thinking_level),
            session_service=InMemorySessionService(),
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run(self, user_key: str, session_id: str | None, message: str, token: str) -> TurnResult:
        from google.genai import types

        from .agent import current_token

        service = self._runner.session_service
        session = None
        if session_id:
            session = await service.get_session(app_name=APP_NAME, user_id=user_key, session_id=session_id)
        if session is None:  # new chat, or another user's / unknown id: never reuse it
            session = await service.create_session(app_name=APP_NAME, user_id=user_key)

        result = TurnResult(session_id=session.id, reply="")
        pending: dict[str, str] = {}
        reset = current_token.set(token)
        try:
            async with self._locks[session.id]:
                async for event in self._runner.run_async(
                    user_id=user_key,
                    session_id=session.id,
                    new_message=types.Content(role="user", parts=[types.Part(text=message)]),
                ):
                    for call in event.get_function_calls():
                        pending[call.id or call.name] = call.name
                    for response in event.get_function_responses():
                        name = pending.pop(response.id or response.name, response.name)
                        result.activity.append(Activity(name, summarize(name, response.response or {})))
                    if event.author != "user" and event.content and event.content.parts and not event.get_function_calls():
                        result.reply += "".join(
                            p.text or "" for p in event.content.parts if not getattr(p, "thought", False)
                        )
        finally:
            current_token.reset(reset)
        result.reply = result.reply.strip()
        return result


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, hits = time.monotonic(), self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def create_app(settings: Settings, verifier: Any = None, engine: ChatEngine | None = None) -> FastAPI:
    verifier = verifier or OidcTokenVerifier(
        OidcSettings(
            issuer=settings.issuer,
            audience=settings.audience,
            jwks_url=settings.issuer.rstrip("/") + "/.well-known/jwks.json",
            tenant_id="shiftmate",
        )
    )
    limiter = RateLimiter(settings.rate_limit, settings.rate_window_seconds)
    app = FastAPI(title="ShiftMate", docs_url=None, redoc_url=None, openapi_url=None)
    state: dict[str, ChatEngine] = {}

    def get_engine() -> ChatEngine:
        if "engine" not in state:
            state["engine"] = engine or AdkChatEngine(settings)
        return state["engine"]

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": APP_NAME}

    @app.get("/config")
    async def config() -> dict[str, str]:
        # Public values only: what the browser needs to start Auth0 sign-in.
        return {"domain": settings.auth0_domain, "clientId": settings.auth0_client_id, "audience": settings.audience}

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> JSONResponse:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        access = await verifier.verify_token(token) if token else None
        if access is None:
            return JSONResponse({"error": "Please sign in again."}, status_code=401)
        user_key = stable_id("u", str((access.claims or {}).get("iss", settings.issuer)), access.subject or "")

        message = body.message.strip()
        if not message:
            return JSONResponse({"error": "Type a message first."}, status_code=400)
        if len(message) > MAX_MESSAGE_CHARS:
            return JSONResponse({"error": f"Messages are limited to {MAX_MESSAGE_CHARS} characters."}, status_code=400)
        if not limiter.allow(user_key):
            return JSONResponse({"error": "Too many messages. Please wait a few minutes."}, status_code=429)

        started = time.monotonic()
        try:
            result = await get_engine().run(user_key, body.session_id, message, token)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "chat_error", "error": type(exc).__name__}), file=sys.stderr, flush=True)
            return JSONResponse({"error": "ShiftMate is unavailable right now. Please try again."}, status_code=503)
        print(
            json.dumps({
                "severity": "INFO",
                "event": "chat_turn",
                "user_id": user_key,
                "tools": [a.tool for a in result.activity],
                "duration_ms": round((time.monotonic() - started) * 1000),
            }),
            file=sys.stderr,
            flush=True,
        )
        return JSONResponse({
            "session_id": result.session_id,
            "reply": result.reply or "(no reply)",
            "activity": [{"tool": a.tool, "summary": a.summary} for a in result.activity],
        })

    return app


def main() -> None:
    import uvicorn

    app = create_app(Settings.from_env())
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
