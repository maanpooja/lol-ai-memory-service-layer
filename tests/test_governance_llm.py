"""Policy helpers, prompt files, GeminiClient retry/validation and config, without GCP."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from memory_mcp.config import DEFAULT_BLOCKED_CATEGORIES, PolicyConfig, load_policy_config
from memory_mcp.governance.policy import Policy, Verdict, has_placeholder, is_expired, normalize
from memory_mcp.governance.redactor import INFO_TYPES
from memory_mcp.llm.client import PROMPT_NAMES, GeminiClient, LlmError, load_prompts
from memory_mcp.llm.schemas import ClassifyResult, ExtractResult
from memory_mcp.models import Memory

# --- policy ------------------------------------------------------------------


def judge(**overrides: Any) -> Verdict:
    args = {"fact": "Likes tea", "category": "preference", "confidence": 0.9,
            "sensitivity": "none", "authority_claim": False} | overrides
    return Policy(PolicyConfig()).judge(**args)


def test_policy_verdicts() -> None:
    assert judge() is Verdict.KEEP
    assert judge(sensitivity="health") is Verdict.SENSITIVE
    assert judge(sensitivity=" None ") is Verdict.KEEP
    assert judge(category="salary") is Verdict.SENSITIVE
    assert judge(fact="Email is [EMAIL_ADDRESS]") is Verdict.SENSITIVE
    assert judge(authority_claim=True) is Verdict.REJECTED
    assert judge(category="hobby") is Verdict.REJECTED
    assert judge(confidence=0.5) is Verdict.REJECTED
    assert judge(confidence=float("nan")) is Verdict.REJECTED


def test_placeholder_and_normalize() -> None:
    assert has_placeholder("call [PHONE_NUMBER] now")
    assert not has_placeholder("uses [x] brackets") and not has_placeholder("Likes C[++]")
    assert normalize("  Uses a   Pixel 8. ") == normalize("uses a pixel 8")


def test_ttl_and_expiry() -> None:
    policy = Policy(PolicyConfig(ttl_days={"project": 30, "general": 0}))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert policy.expires_at("project", now) == "2026-01-31T00:00:00.000000Z"
    assert policy.expires_at("general", now) is None
    assert policy.expires_at("unknown", now) is None

    def memory(expires_at: str | None) -> Memory:
        return Memory("i", "t", "u", "f", "project", 0.9, "a", "c", "u", expires_at)

    assert is_expired(memory("2025-12-31T00:00:00.000000Z"), now)
    assert not is_expired(memory("2026-02-01T00:00:00.000000Z"), now)
    assert not is_expired(memory(None), now)
    assert not is_expired(memory("not a date"), now)


def test_info_types_are_unique_and_plain() -> None:
    assert len(INFO_TYPES) == len(set(INFO_TYPES))
    assert all(name.isupper() and " " not in name for name in INFO_TYPES)
    assert "LOCATION" not in INFO_TYPES, "the user's own city must survive redaction"


# --- prompts -----------------------------------------------------------------


def test_prompts_load_and_mark_data_as_untrusted() -> None:
    prompts = load_prompts()
    assert set(prompts) == set(PROMPT_NAMES)
    for name, text in prompts.items():
        assert "<untrusted_data>" in text and "</untrusted_data>" in text, name
        assert "never instructions" in text, name
        assert "Output JSON" in text, name


# --- GeminiClient ------------------------------------------------------------


class FakeModels:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = outputs
        self.requests: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(text=output)


def gemini(outputs: list[Any]) -> tuple[GeminiClient, FakeModels]:
    models = FakeModels(outputs)
    prompts = {name: f"prompt {name}" for name in PROMPT_NAMES}
    client = GeminiClient("p", "us-central1", "gemini-test", prompts=prompts, client=SimpleNamespace(models=models))
    return client, models


def test_gemini_sends_json_mode_temperature_zero_and_system_prompt() -> None:
    client, models = gemini(['{"sensitivity": "none", "authority_claim": false}'])
    result = client.generate("classify", "<untrusted_data>x</untrusted_data>", ClassifyResult)
    assert result == ClassifyResult(sensitivity="none", authority_claim=False)
    (request,) = models.requests
    config = request["config"]
    assert request["model"] == "gemini-test"
    assert request["contents"] == "<untrusted_data>x</untrusted_data>"
    assert config.temperature == 0.0
    assert config.response_mime_type == "application/json"
    assert config.response_schema is ClassifyResult
    assert config.system_instruction == "prompt classify"
    assert config.thinking_config.thinking_level == "MINIMAL"


def test_gemini_retries_once_on_invalid_json() -> None:
    client, models = gemini(["not json", '{"facts": []}'])
    assert client.generate("extract", "d", ExtractResult) == ExtractResult(facts=[])
    assert len(models.requests) == 2


@pytest.mark.parametrize(
    "outputs",
    [
        ["not json", "{still not json"],
        ['{"facts": "wrong type"}', '{"facts": [{"fact": 1}]}'],
        [RuntimeError("quota"), None],
    ],
)
def test_gemini_gives_up_after_the_retry(outputs: list[Any]) -> None:
    client, models = gemini(outputs)
    with pytest.raises(LlmError) as excinfo:
        client.generate("extract", "secret user text", ExtractResult)
    assert "secret user text" not in str(excinfo.value)
    assert len(models.requests) == 2


# --- config ------------------------------------------------------------------


def test_policy_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MAX_INPUT_CHARS", "MAX_FACTS_PER_CALL", "MIN_CONFIDENCE", "BLOCKED_CATEGORIES"):
        monkeypatch.delenv(name, raising=False)
    assert load_policy_config() == PolicyConfig()
    assert PolicyConfig().blocked_categories == DEFAULT_BLOCKED_CATEGORIES

    monkeypatch.setenv("MIN_CONFIDENCE", "0.8")
    monkeypatch.setenv("BLOCKED_CATEGORIES", "Health, salary ,")
    monkeypatch.setenv("TTL_DAYS_PROJECT", "7")
    config = load_policy_config()
    assert config.min_confidence == 0.8
    assert config.blocked_categories == ("health", "salary")
    assert config.ttl_days["project"] == 7 and config.ttl_days["profile"] == 365

    monkeypatch.setenv("MAX_INPUT_CHARS", "lots")
    with pytest.raises(RuntimeError, match="MAX_INPUT_CHARS"):
        load_policy_config()
