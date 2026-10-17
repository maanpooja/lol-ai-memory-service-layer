"""Test doubles: an in-memory store with the real isolation rules, plus redactor and LLM fakes."""

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from memory_mcp.governance.redactor import Redaction, RedactorError
from memory_mcp.llm.client import LlmError
from memory_mcp.models import Memory, utc_now_iso
from memory_mcp.store.base import MemoryStore


class FakeMemoryStore(MemoryStore):
    def __init__(self) -> None:
        self.memories: dict[str, Memory] = {}

    def _owned(self, tenant_id: str, user_id: str) -> list[Memory]:
        return [
            m for m in self.memories.values() if m.tenant_id == tenant_id and m.user_id == user_id
        ]

    def create(
        self,
        tenant_id: str,
        user_id: str,
        fact: str,
        category: str,
        confidence: float,
        source_app: str,
        expires_at: str | None = None,
    ) -> Memory:
        now = utc_now_iso()
        memory = Memory(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            fact=fact,
            category=category,
            confidence=confidence,
            source_app=source_app,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        self.memories[memory.id] = memory
        return memory

    def get(self, tenant_id: str, user_id: str, memory_id: str) -> Memory | None:
        memory = self.memories.get(memory_id)
        if memory is None or (memory.tenant_id, memory.user_id) != (tenant_id, user_id):
            return None
        return memory

    def update(self, tenant_id: str, user_id: str, memory_id: str, fact: str) -> Memory:
        memory = self.get(tenant_id, user_id, memory_id)
        if memory is None:
            raise PermissionError(f"memory {memory_id} not found or not owned by caller")
        updated = Memory(**{**memory.__dict__, "fact": fact, "updated_at": utc_now_iso()})
        self.memories[memory_id] = updated
        return updated

    def delete(self, tenant_id: str, user_id: str, memory_id: str) -> bool:
        if self.get(tenant_id, user_id, memory_id) is None:
            return False
        del self.memories[memory_id]
        return True

    def search(self, tenant_id: str, user_id: str, query: str, limit: int = 10) -> list[Memory]:
        words = query.lower().split()
        hits = [
            m for m in self._owned(tenant_id, user_id) if any(w in m.fact.lower() for w in words)
        ]
        return hits[:limit]

    def list_all(self, tenant_id: str, user_id: str, limit: int = 100) -> list[Memory]:
        return self._owned(tenant_id, user_id)[:limit]


# --- Part 3: redactor and LLM fakes ----------------------------------------

_FAKE_PII = [
    ("EMAIL_ADDRESS", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("PHONE_NUMBER", re.compile(r"\+?\d[\d -]{8,}\d")),
    ("PASSWORD", re.compile(r"(?i)(?<=password is )\S+|(?<=password: )\S+|hunter2")),
]


class FakeRedactor:
    """Regex redaction of emails, phones and "hunter2"-style passwords. Records every input."""

    def __init__(self) -> None:
        self.fail = False
        self.inputs: list[str] = []

    def redact(self, text: str) -> Redaction:
        self.inputs.append(text)
        if self.fail:
            raise RedactorError("simulated DLP outage")
        findings = 0
        for name, pattern in _FAKE_PII:
            text, count = pattern.subn(f"[{name}]", text)
            findings += count
        return Redaction(text=text, findings=findings)


Responder = Callable[[str], Any]  # receives the data sent to the model


def _payload(data: str) -> dict[str, Any]:
    """The JSON between the untrusted_data delimiters (decide and relevance prompts)."""
    return json.loads(data.removeprefix("<untrusted_data>").removesuffix("</untrusted_data>"))


def default_decide(data: str) -> dict[str, Any]:
    facts = _payload(data)["new_facts"]
    return {"decisions": [{"fact_index": f["fact_index"], "op": "ADD", "target": "", "text": f["fact"]} for f in facts]}


def default_relevance(data: str) -> dict[str, Any]:
    return {"refs": [m["ref"] for m in _payload(data)["memories"]]}


class FakeLlm:
    """Scripted model. Each prompt has a responder; `calls` records every (prompt, data).

    A responder may return a dict, a pydantic model, or raise. Set `fail` to a
    prompt name (or "*") to make that prompt raise LlmError.
    """

    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail: str | None = None
        self.responders: dict[str, Responder] = {
            "extract": lambda data: {"facts": []},
            "decide": default_decide,
            "relevance": default_relevance,
            "classify": lambda data: {"sensitivity": "none", "authority_claim": False},
        }

    def on(self, prompt: str, response: Any) -> None:
        """Script a prompt with a fixed response or a responder function."""
        self.responders[prompt] = response if callable(response) else (lambda data: response)

    def extracts(self, *facts: dict[str, Any]) -> None:
        """Shortcut: the extract prompt returns these facts (missing fields get safe defaults)."""
        defaults = {"category": "profile", "confidence": 0.9, "sensitivity": "none", "authority_claim": False}
        self.on("extract", {"facts": [{**defaults, **f} for f in facts]})

    def prompts(self, name: str) -> list[str]:
        return [data for prompt, data in self.calls if prompt == name]

    def generate(self, prompt: str, data: str, schema: type[BaseModel]) -> Any:
        self.calls.append((prompt, data))
        if self.fail in (prompt, "*"):
            raise LlmError(f"{prompt} call failed after 2 attempts: simulated")
        response = self.responders[prompt](data)
        return response if isinstance(response, schema) else schema.model_validate(response)


class CountingStore(FakeMemoryStore):
    """FakeMemoryStore that records every method call, and can hide recent writes from search."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.unindexed: set[str] = set()  # ids that search cannot see yet

    def create(self, *args: Any, **kwargs: Any) -> Memory:
        self.calls.append("create")
        return super().create(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Memory:
        self.calls.append("update")
        return super().update(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append("delete")
        return super().delete(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Memory | None:
        self.calls.append("get")
        return super().get(*args, **kwargs)

    def search(self, tenant_id: str, user_id: str, query: str, limit: int = 10) -> list[Memory]:
        self.calls.append("search")
        hits = super().search(tenant_id, user_id, query, limit=1000)
        return [m for m in hits if m.id not in self.unindexed][:limit]

    def list_all(self, *args: Any, **kwargs: Any) -> list[Memory]:
        self.calls.append("list_all")
        return super().list_all(*args, **kwargs)
