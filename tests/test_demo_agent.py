"""Part 5 demo agent: loads cleanly, has only the MCP toolset, and handles tokens safely. No network."""

import importlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("google.adk", reason="needs the agent dependency group: uv run --group agent pytest")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The package loads the agent on import: give it a URL and a throwaway token cache first,
# so the tests never touch the real cache or network.
os.environ["MEMORY_MCP_URL"] = "https://memory.example.test/mcp"
os.environ["DEMO_TOKEN_CACHE"] = tempfile.mkdtemp(prefix="demo-token-cache-")

from demo_agent.memory_agent import auth  # noqa: E402

TOKEN = "eyJ.synthetic-test-token.sig"
URL = "https://memory.example.test/mcp"


@pytest.fixture(autouse=True)
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MEMORY_MCP_URL", URL)
    monkeypatch.setenv("DEMO_TOKEN_CACHE", str(tmp_path))
    monkeypatch.setenv("DEMO_TENANT_ID", "demo-tenant")
    monkeypatch.setenv("DEMO_USER_ID", "alice")
    return tmp_path


def load_agent_module():
    import demo_agent.memory_agent.agent as module

    return importlib.reload(module)


# --- agent -------------------------------------------------------------------


def test_root_agent_has_exactly_the_mcp_toolset() -> None:
    from google.adk.tools.mcp_tool import McpToolset

    module = load_agent_module()
    agent = module.root_agent
    assert agent.name == "memory_agent"
    assert agent.description
    assert len(agent.tools) == 1 and isinstance(agent.tools[0], McpToolset)
    assert agent.tools[0].tool_filter is None, "all five tools must be exposed"
    assert agent.model == "gemini-3.5-flash"


def test_package_import_exposes_root_agent() -> None:
    import demo_agent.memory_agent as package

    assert package.agent.root_agent.name == "memory_agent"


def test_instruction_covers_the_demo_rules() -> None:
    text = load_agent_module().root_agent.instruction
    for needle in ("list_memories", "search_memory", "add_memory", "delete_memory", "Context only"):
        assert needle in text
    assert "confirm" in text.lower(), "delete_memory needs a user confirmation"


def test_missing_url_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_MCP_URL")
    with pytest.raises(RuntimeError, match="MEMORY_MCP_URL is not set. Copy demo_agent/.env.example"):
        load_agent_module()
    monkeypatch.setenv("MEMORY_MCP_URL", "https://memory.example.test/")
    with pytest.raises(RuntimeError, match="must end with /mcp"):
        load_agent_module()


def test_missing_token_warns_but_loads(capsys: pytest.CaptureFixture[str]) -> None:
    assert load_agent_module().root_agent is not None
    assert "scripts/demo_login.py --user alice" in capsys.readouterr().err


# --- auth --------------------------------------------------------------------


class MockTokenSource:
    def __init__(self) -> None:
        self.calls = 0

    def token(self) -> auth.Secret:
        self.calls += 1
        return auth.Secret(TOKEN)


def test_header_provider_sends_a_fresh_bearer_token_and_ignores_adk_user() -> None:
    source = MockTokenSource()
    provide = auth.header_provider(source)

    class AdkContext:
        user_id = "user"  # adk web's generic id; must not matter

    assert provide(AdkContext()) == {"Authorization": f"Bearer {TOKEN}"}
    provide(AdkContext())
    assert source.calls == 2, "a token is fetched per request, so a re-login applies without restart"


def test_tokens_never_show_in_repr_or_logs(caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    identity = auth.DemoIdentity("demo-tenant", "alice")
    path = auth.save_token(identity, TOKEN, 3600, "alice@demo.test")
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    source = auth.CachedTokenSource(identity)
    with caplog.at_level(logging.DEBUG):
        token = source.token()
        logging.getLogger("demo").info("source=%r token=%r", source, token)
        print(repr(source), repr(token))
    assert token == TOKEN
    assert TOKEN not in repr(source) and TOKEN not in repr(token)
    assert TOKEN not in caplog.text
    assert TOKEN not in capsys.readouterr().out


def test_expired_missing_and_foreign_tokens_are_refused(demo_env: Path) -> None:
    alice = auth.DemoIdentity("demo-tenant", "alice")
    with pytest.raises(auth.DemoAuthError, match="No token for demo user 'alice'"):
        auth.CachedTokenSource(alice).token()

    auth.save_token(alice, TOKEN, 3600, "alice@demo.test")
    later = auth.CachedTokenSource(alice, now=lambda: time.time() + 3600)
    with pytest.raises(auth.DemoAuthError, match="expired") as excinfo:
        later.token()
    assert TOKEN not in str(excinfo.value)

    # A cache file that claims another identity (e.g. copied bob -> alice) is refused.
    path = auth.token_path(alice)
    entry = json.loads(path.read_text()) | {"user_id": "bob"}
    path.write_text(json.dumps(entry))
    with pytest.raises(auth.DemoAuthError, match="different identity"):
        auth.CachedTokenSource(alice).token()


def test_identity_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_USER_ID", "bob")
    assert auth.DemoIdentity.from_env() == auth.DemoIdentity("demo-tenant", "bob")
    monkeypatch.delenv("DEMO_USER_ID")
    monkeypatch.delenv("DEMO_TENANT_ID")
    assert auth.DemoIdentity.from_env() == auth.DemoIdentity("demo-tenant", "alice")
