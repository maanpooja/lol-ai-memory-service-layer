"""Configuration loaded from environment variables (and a .env file)."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

SERVING_CONFIG_ID = "default_search"
BRANCH_ID = "default_branch"

# Categories a memory may have. Everything else is rejected in code.
CATEGORIES = ("profile", "preference", "device", "project", "general")

DEFAULT_BLOCKED_CATEGORIES = (
    "health",
    "financial",
    "credentials",
    "salary",
    "performance_review",
    "grievance",
    "legal",
    "government_id",
    "biometric",
    "religion",
    "political",
    "sexual_orientation",
    "minors_data",
)

DEFAULT_TTL_DAYS = {"preference": 180, "profile": 365, "device": 365, "project": 30, "general": 90}


@dataclass(frozen=True)
class PolicyConfig:
    """Limits and rules applied in code to everything the LLM proposes."""

    max_input_chars: int = 8000
    max_facts_per_call: int = 10
    min_confidence: float = 0.6
    blocked_categories: tuple[str, ...] = DEFAULT_BLOCKED_CATEGORIES
    ttl_days: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TTL_DAYS))


@dataclass(frozen=True)
class AiConfig:
    """Where the Gemini and DLP calls go."""

    gemini_model: str = "gemini-3.5-flash"
    gemini_location: str = "global"  # gemini-3.5-flash generates only via the global endpoint
    # MINIMAL: 1-2s per call with the same answers on our prompts; default thinking took 5-88s.
    gemini_thinking_level: str = "MINIMAL"
    dlp_location: str = "global"


@dataclass(frozen=True)
class Config:
    project_id: str
    location: str = "global"
    data_store_id: str = "memories"
    collection: str = "default_collection"
    engine_id: str = ""  # optional; when set, search goes through the search app
    ai: AiConfig = field(default_factory=AiConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    @property
    def collection_path(self) -> str:
        return (
            f"projects/{self.project_id}/locations/{self.location}"
            f"/collections/{self.collection}"
        )

    @property
    def data_store_path(self) -> str:
        return f"{self.collection_path}/dataStores/{self.data_store_id}"

    @property
    def branch_path(self) -> str:
        return f"{self.data_store_path}/branches/{BRANCH_ID}"

    @property
    def schema_path(self) -> str:
        return f"{self.data_store_path}/schemas/default_schema"

    @property
    def engine_path(self) -> str:
        return f"{self.collection_path}/engines/{self.engine_id}"

    @property
    def serving_config_path(self) -> str:
        parent = self.engine_path if self.engine_id else self.data_store_path
        return f"{parent}/servingConfigs/{SERVING_CONFIG_ID}"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_number(name: str, default: float, kind: type) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None


def load_policy_config() -> PolicyConfig:
    blocked = os.environ.get("BLOCKED_CATEGORIES", "").strip()
    return PolicyConfig(
        max_input_chars=int(_env_number("MAX_INPUT_CHARS", 8000, int)),
        max_facts_per_call=int(_env_number("MAX_FACTS_PER_CALL", 10, int)),
        min_confidence=float(_env_number("MIN_CONFIDENCE", 0.6, float)),
        blocked_categories=(
            tuple(c.strip().lower() for c in blocked.split(",") if c.strip())
            if blocked
            else DEFAULT_BLOCKED_CATEGORIES
        ),
        ttl_days={
            category: int(_env_number(f"TTL_DAYS_{category.upper()}", days, int))
            for category, days in DEFAULT_TTL_DAYS.items()
        },
    )


def load_config() -> Config:
    load_dotenv()
    project_id = os.environ.get("PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError("PROJECT_ID is not set. Copy .env.example to .env and fill it in.")
    return Config(
        project_id=project_id,
        location=_env("DE_LOCATION", "global"),
        data_store_id=_env("DATA_STORE_ID", "memories"),
        collection=_env("COLLECTION", "default_collection"),
        engine_id=os.environ.get("ENGINE_ID", "").strip(),
        ai=AiConfig(
            gemini_model=_env("GEMINI_MODEL", AiConfig.gemini_model),
            gemini_location=_env("GEMINI_LOCATION", AiConfig.gemini_location),
            gemini_thinking_level=_env("GEMINI_THINKING_LEVEL", AiConfig.gemini_thinking_level).upper(),
            dlp_location=_env("DLP_LOCATION", AiConfig.dlp_location),
        ),
        policy=load_policy_config(),
    )
