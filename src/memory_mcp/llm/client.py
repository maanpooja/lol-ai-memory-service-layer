"""LLM access: one JSON-mode call per pipeline step, validated against a pydantic schema."""

from importlib import resources
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

PROMPT_NAMES = ("extract", "decide", "relevance", "classify")
GEMINI_TIMEOUT_MS = 30_000
ATTEMPTS = 2  # the first call plus one retry

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    """The model call failed or its output was invalid after the retry. Never carries content."""


def load_prompts() -> dict[str, str]:
    """Read prompts/*.md once, at startup. A missing or empty prompt is a startup error."""
    folder = resources.files("memory_mcp") / "prompts"
    prompts = {name: (folder / f"{name}.md").read_text(encoding="utf-8") for name in PROMPT_NAMES}
    empty = [name for name, text in prompts.items() if not text.strip()]
    if empty:
        raise RuntimeError(f"empty prompt file(s): {', '.join(empty)}")
    return prompts


class LlmClient(Protocol):
    model: str

    def generate(self, prompt: str, data: str, schema: type[T]) -> T:
        """Run prompt `prompt` (a PROMPT_NAMES entry) over `data` and return parsed output.

        Raises LlmError on failure. Implementations retry once on invalid output.
        """
        ...


class GeminiClient:
    """Gemini on Vertex AI, temperature 0, JSON output constrained by the schema."""

    def __init__(
        self,
        project_id: str,
        location: str,
        model: str,
        prompts: dict[str, str] | None = None,
        client: Any = None,
        thinking_level: str | None = "MINIMAL",
    ) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self.model = model
        # None or "" leaves the model's default thinking on (slow and variable for these prompts).
        self._thinking = (
            types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level))
            if thinking_level
            else None
        )
        self._prompts = prompts if prompts is not None else load_prompts()
        self._client = client or genai.Client(
            vertexai=True,
            project=project_id,
            location=location,
            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
        )

    def generate(self, prompt: str, data: str, schema: type[T]) -> T:
        config = self._types.GenerateContentConfig(
            system_instruction=self._prompts[prompt],
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=self._thinking,
        )
        failure = "no attempt made"
        for _ in range(ATTEMPTS):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=data, config=config
                )
                return schema.model_validate_json(response.text or "")
            except ValidationError:
                failure = "output did not match the schema"
            except Exception as exc:  # SDK, network or quota errors
                failure = type(exc).__name__
        raise LlmError(f"{prompt} call failed after {ATTEMPTS} attempts: {failure}")
