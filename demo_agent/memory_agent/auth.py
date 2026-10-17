"""Auth headers for the deployed memory-mcp server, for the demo identity in env.

The server (Part 4) verifies Auth0 access tokens: tenant = the deployment's
TENANT_ID, user = a hash of the token's subject. So the demo identity is chosen
by WHICH Auth0 user signed in, not by anything this agent sends:

    DEMO_USER_ID=alice  ->  token cached by `scripts/demo_login.py --user alice`

The token is read from the cache on every MCP request (ADK header_provider), so
signing in again during a demo takes effect without restarting `adk web`.
ADK's own user_id is ignored. Tokens are never printed or logged.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

EXPIRY_MARGIN_SECONDS = 60
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "memory-mcp" / "tokens"


class DemoAuthError(RuntimeError):
    """No usable token for the demo identity. The message says how to fix it; it never holds a token."""


@dataclass(frozen=True)
class DemoIdentity:
    tenant_id: str
    user_id: str

    @classmethod
    def from_env(cls) -> "DemoIdentity":
        return cls(
            tenant_id=os.environ.get("DEMO_TENANT_ID", "").strip() or "demo-tenant",
            user_id=os.environ.get("DEMO_USER_ID", "").strip() or "alice",
        )


class Secret(str):
    """A str that hides itself in repr/str formatting of containers and logs."""

    def __repr__(self) -> str:
        return "Secret(***)"


def cache_dir() -> Path:
    return Path(os.environ.get("DEMO_TOKEN_CACHE", "").strip() or DEFAULT_CACHE_DIR)


def token_path(identity: DemoIdentity) -> Path:
    return cache_dir() / f"{identity.tenant_id}__{identity.user_id}.json"


def login_hint(identity: DemoIdentity) -> str:
    return (
        f"Sign in first: uv run --group agent python scripts/demo_login.py --user {identity.user_id}"
    )


class TokenSource(Protocol):
    def token(self) -> str: ...


class CachedTokenSource:
    """Reads the token that scripts/demo_login.py cached for this identity."""

    def __init__(self, identity: DemoIdentity, now: Any = time.time) -> None:
        self.identity = identity
        self._now = now

    def token(self) -> Secret:
        path = token_path(self.identity)
        try:
            entry = json.loads(path.read_text())
        except FileNotFoundError:
            raise DemoAuthError(f"No token for demo user {self.identity.user_id!r}. {login_hint(self.identity)}") from None
        except (OSError, ValueError):
            raise DemoAuthError(f"Unreadable token cache {path}. {login_hint(self.identity)}") from None
        if entry.get("tenant_id") != self.identity.tenant_id or entry.get("user_id") != self.identity.user_id:
            raise DemoAuthError(f"Token cache {path} is for a different identity. {login_hint(self.identity)}")
        if float(entry.get("expires_at", 0)) - EXPIRY_MARGIN_SECONDS <= self._now():
            raise DemoAuthError(f"Token for demo user {self.identity.user_id!r} has expired. {login_hint(self.identity)}")
        return Secret(entry["access_token"])

    def __repr__(self) -> str:
        return f"CachedTokenSource({self.identity.tenant_id}/{self.identity.user_id})"


def save_token(identity: DemoIdentity, access_token: str, expires_in: int, email: str | None) -> Path:
    """Cache a token readable only by the current OS user. Used by scripts/demo_login.py."""
    path = token_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = {
        "tenant_id": identity.tenant_id,
        "user_id": identity.user_id,
        "email": email,
        "expires_at": time.time() + int(expires_in),
        "access_token": access_token,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(entry, file)
    os.chmod(path, 0o600)
    return path


def auth_headers(source: TokenSource) -> dict[str, str]:
    return {"Authorization": f"Bearer {source.token()}"}


def header_provider(source: TokenSource):
    """ADK McpToolset header_provider: a fresh token per MCP request. The ADK context is ignored."""

    def provide(_context: Any) -> dict[str, str]:
        return auth_headers(source)

    return provide
