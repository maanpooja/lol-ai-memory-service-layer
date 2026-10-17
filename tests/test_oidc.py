"""OIDC bearer-token auth, with locally generated keys. No network, no real issuer."""

import asyncio
import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fakes import FakeLlm, FakeMemoryStore, FakeRedactor
from starlette.testclient import TestClient

from memory_mcp.identity import get_identity_provider
from memory_mcp.oidc import OidcIdentityProvider, OidcSettings, OidcTokenVerifier, stable_id
from memory_mcp.server import create_server

ISSUER = "https://tenant.example-idp.test/"
AUDIENCE = "https://memory-api.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SETTINGS = OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url="unused", tenant_id="acme")


def token(key: Any = KEY, algorithm: str = "RS256", drop: tuple[str, ...] = (), **claims: Any) -> str:
    now = int(time.time())
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "auth0|alice", "azp": "agent-client",
            "iat": now, "exp": now + 300, "scope": "memory:rw"} | claims
    for name in drop:
        body.pop(name)
    return jwt.encode(body, key, algorithm=algorithm)


def verifier(settings: OidcSettings = SETTINGS) -> OidcTokenVerifier:
    return OidcTokenVerifier(settings, key_resolver=lambda _token: KEY.public_key())


def verify(raw: str, settings: OidcSettings = SETTINGS) -> Any:
    return asyncio.run(verifier(settings).verify_token(raw))


# --- token verification ------------------------------------------------------


def test_valid_token_is_accepted() -> None:
    access = verify(token())
    assert access.subject == "auth0|alice"
    assert access.client_id == "agent-client"
    assert access.scopes == ["memory:rw"]
    assert access.claims == {"iss": ISSUER}


@pytest.mark.parametrize(
    "raw",
    [
        token(exp=int(time.time()) - 120),  # expired beyond the leeway
        token(aud="https://someone-else.test"),
        token(iss="https://evil.test/"),
        token(key=OTHER_KEY),  # signed by a key the issuer does not publish
        token(drop=("sub",)),
        token(drop=("exp",)),
        token(drop=("aud",)),
        "not-a-jwt",
        "",
    ],
    ids=["expired", "wrong-aud", "wrong-iss", "wrong-key", "no-sub", "no-exp", "no-aud", "garbage", "empty"],
)
def test_invalid_tokens_are_rejected(raw: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert verify(raw) is None
    err = capsys.readouterr().err
    assert "rejected bearer token" in err
    if raw:
        assert raw not in err, "tokens are never logged"


def test_algorithm_confusion_and_none_are_rejected() -> None:
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "iat": 1, "exp": 2**31}, None, algorithm="none")
    assert verify(unsigned) is None
    # HS256 "signed" with a guessable secret must not pass as RS256.
    assert verify(token(key="a-guessable-shared-secret-of-32-bytes!", algorithm="HS256")) is None


def test_required_scopes() -> None:
    strict = OidcSettings(ISSUER, AUDIENCE, "unused", "acme", required_scopes=("memory:rw",))
    assert verify(token(), strict) is not None
    assert verify(token(scope="read:other"), strict) is None


def test_user_id_is_a_stable_filter_safe_hash() -> None:
    a = stable_id("u", ISSUER, "auth0|alice")
    assert a == stable_id("u", ISSUER, "auth0|alice")
    assert a != stable_id("u", "https://other-issuer/", "auth0|alice"), "same sub, other issuer = other user"
    assert a.startswith("u-") and "|" not in a and "alice" not in a


# --- provider selection ------------------------------------------------------


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("TENANT_ID", "acme")
    for name in ("OIDC_JWKS_URL", "OIDC_ALGORITHMS", "OIDC_REQUIRED_SCOPES", "SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_factory_builds_oidc_provider_for_http(oidc_env: pytest.MonkeyPatch) -> None:
    provider = get_identity_provider("http")
    assert isinstance(provider, OidcIdentityProvider)
    assert provider.settings.jwks_url == "https://tenant.example-idp.test/.well-known/jwks.json"
    assert provider.settings.algorithms == ("RS256",)


def test_factory_refuses_unsafe_oidc_setups(oidc_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="transport http"):
        get_identity_provider("stdio")
    oidc_env.setenv("OIDC_ALGORITHMS", "RS256,HS256")
    with pytest.raises(RuntimeError, match="asymmetric"):
        get_identity_provider("http")
    oidc_env.delenv("OIDC_ALGORITHMS")
    oidc_env.setenv("TENANT_ID", "acme corp")
    with pytest.raises(RuntimeError, match="TENANT_ID"):
        get_identity_provider("http")
    oidc_env.delenv("TENANT_ID")
    with pytest.raises(RuntimeError, match="TENANT_ID"):
        get_identity_provider("http")


# --- end to end over HTTP ----------------------------------------------------


@pytest.fixture
def http() -> tuple[TestClient, FakeMemoryStore, FakeLlm]:
    store, llm = FakeMemoryStore(), FakeLlm()
    provider = OidcIdentityProvider(SETTINGS, verifier())
    app = create_server(store, FakeRedactor(), llm, provider).streamable_http_app(stateless_http=True, host="0.0.0.0")  # as main() binds
    with TestClient(app) as client:
        yield client, store, llm


def rpc(client: TestClient, tool: str, arguments: dict[str, Any], bearer: str | None, **headers: str) -> Any:
    sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
    if bearer is not None:
        sent["Authorization"] = f"Bearer {bearer}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    response = client.post("/mcp", json=body, headers=sent)
    if response.status_code != 200:
        return response
    data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
    return json.loads(data)["result"]


def test_requests_without_a_valid_token_get_401(http: tuple[TestClient, FakeMemoryStore, FakeLlm]) -> None:
    client, store, _ = http
    for bearer in (None, "garbage", token(key=OTHER_KEY), token(aud="https://other.test")):
        response = rpc(client, "list_memories", {}, bearer)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
    assert store.memories == {}


def test_health_stays_public(http: tuple[TestClient, FakeMemoryStore, FakeLlm]) -> None:
    client, _, _ = http
    assert client.get("/health").json() == {"status": "ok", "server": "memory-layer"}


def test_identity_comes_from_the_token_and_users_are_isolated(
    http: tuple[TestClient, FakeMemoryStore, FakeLlm],
) -> None:
    client, store, llm = http
    alice, bob = token(sub="auth0|alice"), token(sub="auth0|bob")
    llm.extracts({"fact": "Prefers green tea", "category": "preference"})

    # Dev override headers are ignored in OIDC mode.
    added = rpc(client, "add_memory", {"content": "user: I like green tea"}, alice, **{"X-Dev-User": "bob"})
    memory = store.memories[added["structuredContent"]["saved"][0]["id"]]
    assert memory.tenant_id == "acme"
    assert memory.user_id == stable_id("u", ISSUER, "auth0|alice")
    assert memory.source_app == stable_id("app", ISSUER, "agent-client")

    assert rpc(client, "list_memories", {}, alice)["structuredContent"]["count"] == 1
    assert rpc(client, "list_memories", {}, bob)["structuredContent"]["count"] == 0
    stolen = rpc(client, "delete_memory", {"memory_id": memory.id}, bob)
    assert stolen["isError"] and "Memory not found" in stolen["content"][0]["text"]
    assert memory.id in store.memories
