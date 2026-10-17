"""OIDC bearer-token identity: verify a JWT against the issuer's JWKS, then map it to an Identity.

The MCP SDK's bearer middleware calls `OidcTokenVerifier.verify_token` for every
HTTP request to /mcp and answers 401 itself when it returns None. Tools then
read the verified token through `OidcIdentityProvider`, never from arguments.

- Signature, `iss`, `aud`, `exp`, `iat` and `sub` are required and checked;
  only the configured algorithms are accepted (never "none" or HMAC).
- tenant_id is fixed per deployment (TENANT_ID). user_id is a hash of the
  issuer and `sub`, so it is stable, safe for filters and holds no raw identifier.
- Tokens and claims are never logged; only the failure class is.
"""

import hashlib
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from memory_mcp.identity import Identity

DEFAULT_ALGORITHMS = ("RS256",)
LEEWAY_SECONDS = 30
JWKS_CACHE_SECONDS = 600

KeyResolver = Callable[[str], Any]  # token -> verification key; raises on unknown kid


class NotAuthenticated(Exception):
    """No verified token on this request."""


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    jwks_url: str
    tenant_id: str
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    required_scopes: tuple[str, ...] = ()
    server_url: str | None = None  # public URL of this server, for OAuth metadata

    @classmethod
    def from_env(cls) -> "OidcSettings":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise RuntimeError(f"AUTH_MODE=oidc needs {name}")
            return value

        issuer = required("OIDC_ISSUER")
        algorithms = tuple(
            a.strip() for a in os.environ.get("OIDC_ALGORITHMS", "").split(",") if a.strip()
        ) or DEFAULT_ALGORITHMS
        if any(a.lower() == "none" or a.upper().startswith("HS") for a in algorithms):
            raise RuntimeError("OIDC_ALGORITHMS must be asymmetric (e.g. RS256, ES256)")
        return cls(
            issuer=issuer,
            audience=required("OIDC_AUDIENCE"),
            # Auth0 and most issuers publish keys here; set OIDC_JWKS_URL if yours differs.
            jwks_url=os.environ.get("OIDC_JWKS_URL", "").strip()
            or issuer.rstrip("/") + "/.well-known/jwks.json",
            tenant_id=required("TENANT_ID"),
            algorithms=algorithms,
            required_scopes=tuple(os.environ.get("OIDC_REQUIRED_SCOPES", "").split()),
            server_url=os.environ.get("SERVER_URL", "").strip() or None,
        )


def stable_id(prefix: str, *parts: str) -> str:
    """A filter-safe id derived from opaque identifiers such as "auth0|65f0..."."""
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]
    return f"{prefix}-{digest}"


class OidcTokenVerifier:
    """Implements the MCP SDK TokenVerifier protocol."""

    def __init__(self, settings: OidcSettings, key_resolver: KeyResolver | None = None) -> None:
        self.settings = settings
        if key_resolver is None:
            jwks = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=JWKS_CACHE_SECONDS)
            key_resolver = lambda token: jwks.get_signing_key_from_jwt(token).key  # noqa: E731
        self._resolve_key = key_resolver

    def decode(self, token: str) -> dict[str, Any]:
        """Verify and return the claims. Raises jwt.PyJWTError (or a key lookup error)."""
        key = self._resolve_key(token)
        return jwt.decode(
            token,
            key,
            algorithms=list(self.settings.algorithms),
            audience=self.settings.audience,
            issuer=self.settings.issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # Key lookup may fetch the JWKS over the network; keep it off the event loop.
            claims = await anyio.to_thread.run_sync(self.decode, token)
        except Exception as exc:  # any failure means "not authenticated"
            print(f"WARNING: rejected bearer token: {type(exc).__name__}", file=sys.stderr, flush=True)
            return None
        scopes = str(claims.get("scope", "")).split()
        if any(scope not in scopes for scope in self.settings.required_scopes):
            print("WARNING: rejected bearer token: missing scope", file=sys.stderr, flush=True)
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or "unknown"),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            subject=str(claims["sub"]),
            claims={"iss": claims["iss"]},
        )


class OidcIdentityProvider:
    """Identity from the token the bearer middleware verified for this request."""

    def __init__(self, settings: OidcSettings, verifier: OidcTokenVerifier | None = None) -> None:
        self.settings = settings
        self.verifier = verifier or OidcTokenVerifier(settings)

    @property
    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.settings.issuer,
            resource_server_url=self.settings.server_url,
            required_scopes=list(self.settings.required_scopes) or None,
            validate_token_resource=False,  # the verifier checks `aud` itself
        )

    def identify(self, headers: Mapping[str, str] | None) -> Identity:
        token = get_access_token()
        if token is None or token.subject is None:
            raise NotAuthenticated()
        issuer = str((token.claims or {}).get("iss", self.settings.issuer))
        return Identity(
            tenant_id=self.settings.tenant_id,
            user_id=stable_id("u", issuer, token.subject),
            app_id=stable_id("app", issuer, token.client_id),
        )
