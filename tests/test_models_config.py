from datetime import datetime

import pytest

from memory_mcp.config import Config, load_config
from memory_mcp.models import Memory, utc_now_iso


def make_memory(expires_at: str | None = None) -> Memory:
    return Memory(
        id="3f2b8c1e-9d4a-4f6b-8a1c-2e5d7f9b0c3a",
        tenant_id="t1",
        user_id="u1",
        fact="synthetic fact",
        category="preference",
        confidence=0.8,
        source_app="tests",
        created_at="2026-01-01T00:00:00.000000Z",
        updated_at="2026-01-01T00:00:00.000000Z",
        expires_at=expires_at,
    )


def test_struct_round_trip() -> None:
    memory = make_memory(expires_at="2027-01-01T00:00:00.000000Z")
    assert Memory.from_struct(memory.id, memory.to_struct()) == memory


def test_struct_omits_missing_expiry_and_id() -> None:
    struct = make_memory().to_struct()
    assert "expires_at" not in struct
    assert "id" not in struct
    assert Memory.from_struct("x", struct).expires_at is None


def test_utc_now_iso_is_utc_and_sortable() -> None:
    first, second = utc_now_iso(), utc_now_iso()
    assert first.endswith("Z")
    assert datetime.fromisoformat(first).utcoffset().total_seconds() == 0
    assert first <= second


def test_config_paths() -> None:
    config = Config(project_id="p")
    base = "projects/p/locations/global/collections/default_collection"
    assert config.branch_path == f"{base}/dataStores/memories/branches/default_branch"
    assert config.serving_config_path == f"{base}/dataStores/memories/servingConfigs/default_search"
    with_engine = Config(project_id="p", engine_id="app")
    assert with_engine.serving_config_path == f"{base}/engines/app/servingConfigs/default_search"


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_mcp.config.load_dotenv", lambda: None)
    for name in (
        "DE_LOCATION", "DATA_STORE_ID", "COLLECTION", "ENGINE_ID",
        "GEMINI_MODEL", "GEMINI_LOCATION", "GEMINI_THINKING_LEVEL", "DLP_LOCATION",
        "MAX_INPUT_CHARS", "MAX_FACTS_PER_CALL", "MIN_CONFIDENCE", "BLOCKED_CATEGORIES",
        *(f"TTL_DAYS_{c}" for c in ("PREFERENCE", "PROFILE", "DEVICE", "PROJECT", "GENERAL")),
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROJECT_ID", "my-project")
    assert load_config() == Config(project_id="my-project")


def test_load_config_requires_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memory_mcp.config.load_dotenv", lambda: None)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError):
        load_config()
