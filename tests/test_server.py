"""MCP tool behaviour, using the in-memory fake store. No GCP needed."""

import asyncio
import json
from typing import Any

import pytest
from fakes import FakeLlm, FakeMemoryStore, FakeRedactor
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from memory_mcp.identity import DevIdentityProvider, Identity, current_identity
from memory_mcp.server import SEARCH_NOTE, create_server
from memory_mcp.store.base import StoreError

ALICE = Identity("tenant-1", "alice", "app-a")
BOB = Identity("tenant-1", "bob", "app-b")
ALICE_OTHER_TENANT = Identity("tenant-2", "alice", "app-a")
MISSING_ID = "3f2b8c1e-9d4a-4f6b-8a1c-2e5d7f9b0c3a"
SECRET_FACT = "Synthetic: keeps a pet axolotl named Quibble"


@pytest.fixture
def store() -> FakeMemoryStore:
    return FakeMemoryStore()


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def server(store: FakeMemoryStore, llm: FakeLlm) -> MCPServer:
    return create_server(store, FakeRedactor(), llm)


@pytest.fixture(autouse=True)
def as_alice():
    token = current_identity.set(ALICE)
    yield
    current_identity.reset(token)


def call(server: MCPServer, tool: str, **arguments: Any) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error
    return result.structured_content


def call_error(server: MCPServer, tool: str, **arguments: Any) -> str:
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.call_tool(tool, arguments))
    return str(excinfo.value)


def remember(server: MCPServer, llm: FakeLlm, fact: str, **fields: Any) -> dict[str, Any]:
    """add_memory where the (fake) model extracts exactly `fact`."""
    llm.extracts({"fact": fact, **fields})
    result = call(server, "add_memory", content=f"user: {fact}")
    assert len(result["saved"]) == 1, result
    return result["saved"][0]


def audit_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    captured = capsys.readouterr()
    assert captured.out == "", "nothing may be written to stdout (reserved for stdio transport)"
    return [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]


# --- tool signatures ---------------------------------------------------------


def test_expected_tools_are_registered(server: MCPServer) -> None:
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"add_memory", "search_memory", "update_memory", "delete_memory", "list_memories"}


def test_no_tool_accepts_identity_parameters(server: MCPServer) -> None:
    forbidden = {"tenant_id", "user_id", "app_id", "tenant", "user", "source_app", "ctx"}
    for tool in asyncio.run(server.list_tools()):
        params = set(tool.input_schema.get("properties", {}))
        assert not params & forbidden, f"{tool.name} exposes {params & forbidden}"
        assert tool.description, f"{tool.name} has no description"


def test_identity_arguments_from_the_caller_are_ignored(
    server: MCPServer, store: FakeMemoryStore, llm: FakeLlm
) -> None:
    llm.extracts({"fact": SECRET_FACT})
    added = call(server, "add_memory", content=SECRET_FACT, tenant_id="tenant-2", user_id="bob")["saved"][0]
    stored = store.memories[added["id"]]
    assert (stored.tenant_id, stored.user_id) == ("tenant-1", "alice")


# --- round trip --------------------------------------------------------------


def test_round_trip(server: MCPServer, store: FakeMemoryStore, llm: FakeLlm) -> None:
    added = remember(server, llm, SECRET_FACT, category="profile", confidence=0.7)
    assert added == {"id": added["id"], "op": "ADD", "fact": SECRET_FACT, "category": "profile"}
    assert store.memories[added["id"]].source_app == "app-a"

    found = call(server, "search_memory", query="axolotl")
    assert found["note"] == SEARCH_NOTE
    assert found["selection"] == "gemini"
    assert [m["id"] for m in found["memories"]] == [added["id"]]
    assert set(found["memories"][0]) == {"id", "fact", "category", "confidence", "updated_at"}
    assert found["memories"][0]["confidence"] == 0.7

    listed = call(server, "list_memories")
    assert listed["count"] == 1
    assert listed["memories"][0]["id"] == added["id"]

    updated = call(server, "update_memory", memory_id=added["id"], new_fact="Synthetic: has a cat")
    assert updated["fact"] == "Synthetic: has a cat"
    assert updated["updated_at"] > found["memories"][0]["updated_at"]

    assert call(server, "delete_memory", memory_id=added["id"]) == {"deleted": True}
    assert call(server, "list_memories") == {"memories": [], "count": 0}
    assert call(server, "search_memory", query="cat") == {"memories": [], "note": SEARCH_NOTE, "selection": "none"}


def test_limits_are_clamped(server: MCPServer, store: FakeMemoryStore) -> None:
    for i in range(25):
        store.create("tenant-1", "alice", f"Synthetic: fact number {i}", "general", 0.9, "tests")
    assert len(call(server, "search_memory", query="fact", limit=1000)["memories"]) == 20
    assert len(call(server, "search_memory", query="fact", limit=0)["memories"]) == 1
    assert call(server, "list_memories", limit=-5)["count"] == 1
    assert call(server, "list_memories", limit=1000)["count"] == 25


# --- isolation ---------------------------------------------------------------


@pytest.mark.parametrize("intruder", [BOB, ALICE_OTHER_TENANT])
def test_another_identity_cannot_see_or_touch_a_memory(
    server: MCPServer, store: FakeMemoryStore, llm: FakeLlm, intruder: Identity
) -> None:
    added = remember(server, llm, SECRET_FACT)

    current_identity.set(intruder)
    assert call(server, "list_memories") == {"memories": [], "count": 0}
    assert call(server, "search_memory", query="axolotl")["memories"] == []

    for tool, extra in [("update_memory", {"new_fact": "hijacked"}), ("delete_memory", {})]:
        foreign = call_error(server, tool, memory_id=added["id"], **extra)
        missing = call_error(server, tool, memory_id=MISSING_ID, **extra)
        assert "Memory not found" in foreign
        assert foreign == missing, "a foreign id must look exactly like a missing id"

    assert store.memories[added["id"]].fact == SECRET_FACT


def test_no_identity_means_no_access(server: MCPServer, store: FakeMemoryStore) -> None:
    current_identity.set(None)
    assert "Internal error" in call_error(server, "list_memories")
    assert "Internal error" in call_error(server, "add_memory", content=SECRET_FACT)
    assert store.memories == {}


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "   ", "x" * 8001])
def test_add_memory_rejects_bad_input(
    server: MCPServer, store: FakeMemoryStore, llm: FakeLlm, content: str
) -> None:
    call_error(server, "add_memory", content=content)
    assert store.memories == {}
    assert llm.calls == []


def test_add_memory_accepts_the_longest_content(server: MCPServer, llm: FakeLlm) -> None:
    call(server, "add_memory", content="x" * 8000)
    assert len(llm.calls) == 1


def test_update_and_search_reject_bad_input(server: MCPServer, llm: FakeLlm) -> None:
    added = remember(server, llm, SECRET_FACT)
    call_error(server, "update_memory", memory_id=added["id"], new_fact="")
    call_error(server, "update_memory", memory_id=added["id"], new_fact="x" * 501)
    call_error(server, "search_memory", query="  ")
    assert call(server, "list_memories")["memories"][0]["fact"] == SECRET_FACT


# --- errors and audit log ----------------------------------------------------


def test_store_failures_do_not_leak_internals(
    server: MCPServer, store: FakeMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise StoreError("search failed for tenant=tenant-1: projects/secret-project/internal")

    monkeypatch.setattr(store, "search", boom)
    monkeypatch.setattr(store, "list_all", lambda *a, **k: 1 / 0)
    message = call_error(server, "search_memory", query="anything")
    assert "secret-project" not in message and "unavailable" in message
    assert call_error(server, "list_memories").endswith("Internal error")


def test_audit_log_has_ids_but_no_content(
    server: MCPServer, llm: FakeLlm, capsys: pytest.CaptureFixture[str]
) -> None:
    added = remember(server, llm, SECRET_FACT)
    call(server, "search_memory", query="axolotl Quibble")
    call(server, "update_memory", memory_id=added["id"], new_fact="Synthetic: zebra finch")
    call_error(server, "delete_memory", memory_id=MISSING_ID)
    call_error(server, "add_memory", content="")

    captured = capsys.readouterr()
    assert captured.out == ""
    for content in ("axolotl", "Quibble", "zebra finch"):
        assert content not in captured.err

    lines = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    assert [(r["tool"], r["outcome"]) for r in lines] == [
        ("add_memory", "ok"),
        ("search_memory", "ok"),
        ("update_memory", "ok"),
        ("delete_memory", "denied"),
        ("add_memory", "error"),
    ]
    assert lines[0]["memory_ids"] == [added["id"]]
    assert lines[1]["memory_ids"] == [added["id"]]
    assert lines[3]["memory_ids"] == [MISSING_ID]
    base = {
        "timestamp", "severity", "event", "tool", "tenant_id", "user_id", "app_id",
        "memory_ids", "outcome", "duration_ms",
    }
    counters = {
        "model", "selection", "facts_extracted", "saved", "updated", "skipped",
        "sensitive_blocked", "rejected", "dlp_findings", "llm_calls",
    }
    for record in lines:
        assert (record["tenant_id"], record["user_id"], record["app_id"]) == ("tenant-1", "alice", "app-a")
        expected = base if record["tool"] == "delete_memory" else base | counters
        assert set(record) == expected
    assert lines[0]["saved"] == 1 and lines[0]["llm_calls"] == 1 and lines[0]["model"] == "fake-model"


# --- provider wiring ---------------------------------------------------------


def test_provider_identity_is_used_and_does_not_leak_between_calls(store: FakeMemoryStore) -> None:
    current_identity.set(None)
    llm = FakeLlm()
    server = create_server(store, FakeRedactor(), llm, DevIdentityProvider(BOB))
    added = remember(server, llm, SECRET_FACT)
    stored = store.memories[added["id"]]
    assert (stored.tenant_id, stored.user_id, stored.source_app) == ("tenant-1", "bob", "app-b")
    assert current_identity.get() is None
