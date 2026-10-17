"""Who a request is acting for. Resolved server-side, never from tool arguments.

Two providers, chosen by AUTH_MODE in `get_identity_provider`:
- dev: a STUB that trusts env vars (and optionally headers). Never deploy it.
- oidc: verifies a bearer JWT against the issuer's keys (see oidc.py). HTTP only.
"""

import os
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    user_id: str
    app_id: str


current_identity: ContextVar[Identity | None] = ContextVar("current_identity", default=None)


def get_identity() -> Identity:
    identity = current_identity.get()
    if identity is None:
        raise RuntimeError("no identity set for this request; refusing to touch any memory")
    return identity


class IdentityProvider(Protocol):
    def identify(self, headers: Mapping[str, str] | None) -> Identity:
        """Return the identity for one request. `headers` is None on stdio."""
        ...


class DevIdentityProvider:
    """STUB identity from DEV_* env vars. Not for production."""

    def __init__(self, default: Identity, allow_header_override: bool = False) -> None:
        self.default = default
        self.allow_header_override = allow_header_override

    @classmethod
    def from_env(cls, transport: str) -> "DevIdentityProvider":
        default = Identity(
            tenant_id=os.environ.get("DEV_TENANT_ID", "").strip() or "dev-tenant",
            user_id=os.environ.get("DEV_USER_ID", "").strip() or "dev-user",
            app_id=os.environ.get("DEV_APP_ID", "").strip() or "dev-app",
        )
        wants_override = os.environ.get("DEV_ALLOW_HEADER_OVERRIDE", "").strip().lower() == "true"
        # Headers only exist on HTTP, so the override is never active on stdio.
        return cls(default, allow_header_override=wants_override and transport == "http")

    def identify(self, headers: Mapping[str, str] | None) -> Identity:
        if not self.allow_header_override or headers is None:
            return self.default
        return Identity(
            tenant_id=headers.get("x-dev-tenant") or self.default.tenant_id,
            user_id=headers.get("x-dev-user") or self.default.user_id,
            app_id=headers.get("x-dev-app") or self.default.app_id,
        )


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def get_identity_provider(transport: str) -> IdentityProvider:
    """The one place the identity provider is chosen (AUTH_MODE env var)."""
    mode = os.environ.get("AUTH_MODE", "").strip().lower() or "dev"
    if mode == "oidc":
        from memory_mcp.oidc import OidcIdentityProvider, OidcSettings
        from memory_mcp.store.discovery_engine import validate_id

        if transport != "http":
            raise RuntimeError("AUTH_MODE=oidc needs --transport http (stdio carries no token)")
        settings = OidcSettings.from_env()
        try:
            validate_id(settings.tenant_id, "tenant_id")
        except ValueError:
            raise RuntimeError("TENANT_ID must be 1-128 letters, digits or _ . @ : -") from None
        print(
            f"auth: OIDC issuer={settings.issuer} tenant={settings.tenant_id} "
            f"algorithms={','.join(settings.algorithms)}",
            file=sys.stderr,
            flush=True,
        )
        return OidcIdentityProvider(settings)
    if mode != "dev":
        raise NotImplementedError(f"AUTH_MODE={mode!r} is not implemented; use 'dev' or 'oidc'")

    provider = DevIdentityProvider.from_env(transport)
    who = provider.default
    _warn(
        "STUB identity in use (AUTH_MODE=dev). Not for production. "
        f"Every request acts as tenant={who.tenant_id} user={who.user_id} app={who.app_id}."
    )
    if provider.allow_header_override:
        _warn("=" * 68)
        _warn("DEV_ALLOW_HEADER_OVERRIDE is ON: any client can act as ANY tenant or")
        _warn("user by sending X-Dev-Tenant / X-Dev-User / X-Dev-App headers.")
        _warn("Never expose this server to a network you do not control.")
        _warn("=" * 68)
    return provider
