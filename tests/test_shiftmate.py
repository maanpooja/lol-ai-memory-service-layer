"""ShiftMate web app: auth gate, validation, rate limit, activity summaries. No network, no Gemini."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("google.adk", reason="needs the agent dependency group: uv run --group agent pytest")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402

from memory_mcp.oidc import stable_id  # noqa: E402
from shiftmate.agent import NoTokenForRequest, bearer_header, build_agent, current_token  # noqa: E402
from shiftmate.app import Activity, Settings, TurnResult, create_app, summarize  # noqa: E402
from shiftmate.instructions import INSTRUCTION  # noqa: E402

ISSUER = "https://tenant.example-idp.test/"
SETTINGS = Settings(
    memory_mcp_url="https://memory.example.test/mcp", issuer=ISSUER, audience="https://memory-mcp.example",
    auth0_domain="tenant.example-idp.test", auth0_client_id="spa-client", rate_limit=3,
)
TOKENS = {"alice-token": "auth0|alice", "bob-token": "auth0|bob"}


class StubVerifier:
    async def verify_token(self, token: str):
        sub = TOKENS.get(token)
        if sub is None:
            return None
        return AccessToken(token=token, client_id="spa-client", scopes=[], subject=sub, claims={"iss": ISSUER})


class StubEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str, str]] = []

    async def run(self, user_key, session_id, message, token):
        self.calls.append((user_key, session_id, message, token))
        return TurnResult(session_id=session_id or "s-new", reply="Hello!", activity=[Activity("list_memories", "0 found")])


@pytest.fixture
def engine() -> StubEngine:
    return StubEngine()


@pytest.fixture
def client(engine: StubEngine) -> TestClient:
    return TestClient(create_app(SETTINGS, verifier=StubVerifier(), engine=engine))


def chat(client: TestClient, token: str | None, message: str = "hi", session_id: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/chat", json={"message": message, "session_id": session_id}, headers=headers)


def test_public_pages(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "app": "shiftmate"}
    assert client.get("/config").json() == {
        "domain": "tenant.example-idp.test", "clientId": "spa-client", "audience": "https://memory-mcp.example",
    }
    page = client.get("/")
    assert page.status_code == 200 and "ShiftMate" in page.text and "auth0-spa-js@2.1.3" in page.text


@pytest.mark.parametrize("token", [None, "forged-token"])
def test_chat_requires_a_valid_token(client: TestClient, engine: StubEngine, token: str | None) -> None:
    response = chat(client, token)
    assert response.status_code == 401
    assert engine.calls == [], "nothing runs (no Gemini cost) without a valid sign-in"


def test_chat_runs_as_the_signed_in_user_with_their_own_token(client: TestClient, engine: StubEngine) -> None:
    response = chat(client, "alice-token", "I look after CNC line 3", session_id="s-1")
    assert response.status_code == 200
    assert response.json() == {
        "session_id": "s-1", "reply": "Hello!", "activity": [{"tool": "list_memories", "summary": "0 found"}],
    }
    user_key, session_id, message, token = engine.calls[0]
    assert user_key == stable_id("u", ISSUER, "auth0|alice") and token == "alice-token"
    chat(client, "bob-token")
    assert engine.calls[1][0] == stable_id("u", ISSUER, "auth0|bob")
    assert engine.calls[1][0] != user_key


def test_message_validation_and_rate_limit(client: TestClient, engine: StubEngine) -> None:
    assert chat(client, "alice-token", "   ").status_code == 400
    assert chat(client, "alice-token", "x" * 2001).status_code == 400
    for _ in range(3):
        assert chat(client, "alice-token").status_code == 200
    assert chat(client, "alice-token").status_code == 429
    assert chat(client, "bob-token").status_code == 200, "limits are per user"


def test_engine_failure_is_a_clean_503(client: TestClient, engine: StubEngine, capsys) -> None:
    async def boom(*args):
        raise RuntimeError("secret internal detail")

    engine.run = boom
    response = chat(client, "alice-token", "my private message")
    assert response.status_code == 503 and "secret" not in response.text
    err = capsys.readouterr().err
    assert "my private message" not in err and "alice-token" not in err


def test_turn_log_has_no_content_or_token(client: TestClient, capsys) -> None:
    chat(client, "alice-token", "I injured my wrist")
    err = capsys.readouterr().err
    assert '"event": "chat_turn"' in err
    assert "wrist" not in err and "alice-token" not in err


def test_summaries_for_the_activity_panel() -> None:
    added = {"structuredContent": {
        "saved": [{"op": "UPDATE", "fact": "Works at the Chennai plant"}],
        "skipped_duplicates": 1, "sensitive_not_saved": 1, "rejected": 1,
    }}
    assert summarize("add_memory", added) == (
        "update: Works at the Chennai plant; 1 already known; 1 not saved (privacy); 1 rejected (e.g. authority claim)"
    )
    listed = {"structuredContent": {"memories": [{"fact": "Maintains CNC line 3"}], "count": 1}}
    assert summarize("list_memories", listed) == "1 found: Maintains CNC line 3"
    error = {"isError": True, "content": [{"text": "Error executing tool update_memory: This content can't be stored"}]}
    assert summarize("update_memory", error) == "error: update_memory: This content can't be stored"


def test_header_provider_uses_only_the_current_request_token() -> None:
    with pytest.raises(NoTokenForRequest):
        bearer_header(None)
    reset = current_token.set("alice-token")
    try:
        assert bearer_header(None) == {"Authorization": "Bearer alice-token"}
    finally:
        current_token.reset(reset)
    with pytest.raises(NoTokenForRequest):
        bearer_header(None)


def test_agent_has_only_the_memory_toolset_and_safety_rules() -> None:
    from google.adk.tools.mcp_tool import McpToolset

    agent = build_agent("https://memory.example.test/mcp", "gemini-3.5-flash", "MINIMAL")
    assert agent.name == "shiftmate" and len(agent.tools) == 1 and isinstance(agent.tools[0], McpToolset)
    assert agent.generate_content_config.thinking_config.thinking_level == "MINIMAL"
    for needle in ("list_memories", "search_memory", "add_memory", "delete_memory", "Context only", "lockout/tagout"):
        assert needle in INSTRUCTION
