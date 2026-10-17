"""MemoryService: the add / update / search pipelines over store + redactor + LLM + policy.

Rules this module keeps:
- Raw user text goes through the redactor before any LLM call.
- Writes fail closed: a redactor or LLM failure stores nothing. Reads degrade.
- Every keep/drop decision is made in code (Policy), never by the model.
- The model never sees or returns real memory ids, only short refs (m1, m2, ...)
  that are mapped back here. A ref we did not supply is ignored.
- Nothing here logs or returns content that was blocked.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from memory_mcp.governance.policy import (
    MAX_FACT_CHARS,
    Policy,
    Verdict,
    has_placeholder,
    is_expired,
    is_sensitive_label,
    normalize,
)
from memory_mcp.governance.redactor import Redaction, Redactor, RedactorError
from memory_mcp.identity import Identity
from memory_mcp.llm.client import LlmClient, LlmError
from memory_mcp.llm.schemas import ClassifyResult, DecideResult, ExtractResult, RelevanceResult
from memory_mcp.models import Memory
from memory_mcp.store.base import MemoryStore

logger = logging.getLogger(__name__)

DECIDE_SEARCH_LIMIT = 5  # existing candidates per new fact, from search
DECIDE_RECENT_LIMIT = 50  # recent memories, because search lags writes by about a minute
SEARCH_CANDIDATES = 20
# Store search is keyword based ("lunch" never matches "Is vegetarian"), so search_memory also
# offers the user's recent memories to the model, which selects by meaning.
SEARCH_RECENT_LIMIT = 50

_DELIMITER_RE = re.compile(r"</?\s*untrusted_data\s*>", re.IGNORECASE)

T = TypeVar("T", bound=BaseModel)


class ServiceError(Exception):
    """A failure with a message that is safe to show the caller (no content, no internals)."""


class InvalidInput(ServiceError):
    pass


class ContentBlocked(ServiceError):
    def __init__(self) -> None:
        super().__init__("This content can't be stored")


class Unavailable(ServiceError):
    pass


@dataclass
class PipelineStats:
    """Counters for the audit log. Numbers, the model name and the search mode only, never content."""

    model: str = ""
    selection: str = ""  # search_memory only
    facts_extracted: int = 0
    saved: int = 0
    updated: int = 0
    skipped: int = 0
    sensitive_blocked: int = 0
    rejected: int = 0
    dlp_findings: int = 0
    llm_calls: int = 0

    def to_audit(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SavedFact:
    id: str
    op: str  # "ADD" or "UPDATE"
    fact: str
    category: str


@dataclass
class AddOutcome:
    saved: list[SavedFact] = field(default_factory=list)
    skipped_duplicates: int = 0
    sensitive_not_saved: int = 0
    rejected: int = 0


@dataclass(frozen=True)
class SearchOutcome:
    memories: list[Memory]
    selection: str  # "gemini", "fallback" or "none"


@dataclass(frozen=True)
class _Approved:
    fact: str
    category: str
    confidence: float


def wrap(data: str) -> str:
    """Put untrusted text between delimiters, removing any delimiter it contains itself."""
    return f"<untrusted_data>{_DELIMITER_RE.sub('', data)}</untrusted_data>"


class MemoryService:
    def __init__(self, store: MemoryStore, redactor: Redactor, llm: LlmClient, policy: Policy) -> None:
        self.store = store
        self.redactor = redactor
        self.llm = llm
        self.policy = policy

    # --- helpers ---------------------------------------------------------

    def _redact(self, text: str, stats: PipelineStats, unavailable: str) -> Redaction:
        try:
            redaction = self.redactor.redact(text)
        except RedactorError as exc:
            raise Unavailable(unavailable) from exc
        stats.dlp_findings += redaction.findings
        return redaction

    def _generate(self, prompt: str, data: str, schema: type[T], stats: PipelineStats) -> T:
        stats.model = self.llm.model
        stats.llm_calls += 1
        return self.llm.generate(prompt, data, schema)

    def _clean_input(self, text: str, name: str, max_chars: int) -> str:
        text = text.strip()
        if not text:
            raise InvalidInput(f"{name} must not be empty")
        if len(text) > max_chars:
            raise InvalidInput(f"{name} must be at most {max_chars} characters")
        return text

    def _passes_second_redaction(self, text: str, stats: PipelineStats) -> bool:
        """Second DLP pass on text that will be stored. Catches PII the model paraphrased back in."""
        return self._redact(text, stats, "Personal-data check is unavailable. Nothing was saved.").findings == 0

    # --- add -------------------------------------------------------------

    def add(self, identity: Identity, content: str, stats: PipelineStats) -> AddOutcome:
        not_saved = "Memory processing is unavailable right now. Nothing was saved."
        content = self._clean_input(content, "content", self.policy.config.max_input_chars)
        redacted = self._redact(content, stats, "Personal-data check is unavailable. Nothing was saved.")
        try:
            extracted = self._generate("extract", wrap(redacted.text), ExtractResult, stats).facts
        except LlmError as exc:
            raise Unavailable(not_saved) from exc
        extracted = extracted[: self.policy.config.max_facts_per_call]
        stats.facts_extracted = len(extracted)

        outcome = AddOutcome()
        approved = self._approve(extracted, outcome, stats)
        if not approved:
            return outcome

        existing = self._existing(identity, approved)
        decisions = self._decide(approved, existing, stats, not_saved)
        self._write(identity, approved, existing, decisions, outcome, stats)
        return outcome

    def _approve(self, extracted: list[Any], outcome: AddOutcome, stats: PipelineStats) -> list[_Approved]:
        approved: list[_Approved] = []
        seen: set[str] = set()
        for candidate in extracted:
            fact = candidate.fact.strip()
            category = candidate.category.strip().lower()
            verdict = self.policy.judge(
                fact, category, candidate.confidence, candidate.sensitivity, candidate.authority_claim
            )
            if verdict is Verdict.KEEP:
                key = normalize(fact)
                if key in seen:
                    outcome.skipped_duplicates += 1
                    continue
                if not self._passes_second_redaction(fact, stats):
                    verdict = Verdict.SENSITIVE
                else:
                    seen.add(key)
                    approved.append(_Approved(fact, category, candidate.confidence))
                    continue
            if verdict is Verdict.SENSITIVE:
                outcome.sensitive_not_saved += 1
            else:
                outcome.rejected += 1
        stats.sensitive_blocked = outcome.sensitive_not_saved
        stats.rejected = outcome.rejected
        stats.skipped = outcome.skipped_duplicates
        return approved

    def _existing(self, identity: Identity, approved: list[_Approved]) -> dict[str, Memory]:
        """This user's memories that a new fact could duplicate or update, keyed by ref (m1, m2, ...)."""
        by_id: dict[str, Memory] = {}
        for item in approved:
            for memory in self.store.search(
                identity.tenant_id, identity.user_id, item.fact, DECIDE_SEARCH_LIMIT
            ):
                by_id.setdefault(memory.id, memory)
        # Search lags writes; recent memories close that gap.
        for memory in self.store.list_all(identity.tenant_id, identity.user_id, DECIDE_RECENT_LIMIT):
            by_id.setdefault(memory.id, memory)
        live = [m for m in by_id.values() if not is_expired(m)]
        return {f"m{i}": memory for i, memory in enumerate(live, start=1)}

    def _decide(
        self,
        approved: list[_Approved],
        existing: dict[str, Memory],
        stats: PipelineStats,
        not_saved: str,
    ) -> dict[int, Any]:
        if not existing:
            return {}  # nothing to compare against: every fact is an ADD, no model call needed
        payload = {
            "new_facts": [
                {"fact_index": i, "fact": a.fact, "category": a.category} for i, a in enumerate(approved)
            ],
            "existing": [
                {"ref": ref, "fact": m.fact, "category": m.category} for ref, m in existing.items()
            ],
        }
        try:
            result = self._generate(
                "decide", wrap(json.dumps(payload, ensure_ascii=False)), DecideResult, stats
            )
        except LlmError as exc:
            raise Unavailable(not_saved) from exc
        decisions: dict[int, Any] = {}
        for decision in result.decisions:
            if 0 <= decision.fact_index < len(approved):
                decisions.setdefault(decision.fact_index, decision)  # first decision per fact wins
        return decisions

    def _final_text(self, proposed: str, approved: _Approved, stats: PipelineStats) -> str:
        """Use the model's rewrite for an UPDATE only if it passes the same checks as the fact."""
        proposed = proposed.strip()
        if not proposed or normalize(proposed) == normalize(approved.fact):
            return approved.fact
        if has_placeholder(proposed) or len(proposed) > MAX_FACT_CHARS:
            return approved.fact
        if not self._passes_second_redaction(proposed, stats):
            return approved.fact
        return proposed

    def _write(
        self,
        identity: Identity,
        approved: list[_Approved],
        existing: dict[str, Memory],
        decisions: dict[int, Any],
        outcome: AddOutcome,
        stats: PipelineStats,
    ) -> None:
        updated_targets: set[str] = set()
        for index, item in enumerate(approved):
            decision = decisions.get(index)
            op = decision.op if decision is not None else "ADD"
            if op == "NONE":
                outcome.skipped_duplicates += 1
                continue

            target = existing.get(decision.target) if op == "UPDATE" else None
            if op == "UPDATE" and target is not None and target.id not in updated_targets:
                text = self._final_text(decision.text, item, stats)
                try:
                    memory = self.store.update(identity.tenant_id, identity.user_id, target.id, text)
                except PermissionError:
                    memory = None  # deleted meanwhile: store it as new instead
                if memory is not None:
                    updated_targets.add(memory.id)
                    stats.updated += 1
                    outcome.saved.append(SavedFact(memory.id, "UPDATE", memory.fact, memory.category))
                    continue

            # ADD, or an UPDATE whose target was not one we supplied.
            memory = self.store.create(
                identity.tenant_id,
                identity.user_id,
                item.fact,
                item.category,
                item.confidence,
                identity.app_id,
                self.policy.expires_at(item.category),
            )
            stats.saved += 1
            outcome.saved.append(SavedFact(memory.id, "ADD", memory.fact, memory.category))
        stats.skipped = outcome.skipped_duplicates

    # --- update ----------------------------------------------------------

    def update(self, identity: Identity, memory_id: str, new_fact: str, stats: PipelineStats) -> Memory:
        """Govern the new text, then the ownership-checked store update (PermissionError if not owned)."""
        not_changed = "Memory processing is unavailable right now. Nothing was changed."
        new_fact = self._clean_input(new_fact, "new_fact", MAX_FACT_CHARS)
        redacted = self._redact(new_fact, stats, "Personal-data check is unavailable. Nothing was changed.")
        if redacted.findings or has_placeholder(new_fact):
            stats.sensitive_blocked = 1
            raise ContentBlocked()
        try:
            label = self._generate("classify", wrap(redacted.text), ClassifyResult, stats)
        except LlmError as exc:
            raise Unavailable(not_changed) from exc
        if is_sensitive_label(label.sensitivity):
            stats.sensitive_blocked = 1
            raise ContentBlocked()
        if label.authority_claim:
            stats.rejected = 1
            raise ContentBlocked()
        memory = self.store.update(identity.tenant_id, identity.user_id, memory_id, new_fact)
        stats.updated = 1
        return memory

    # --- search ----------------------------------------------------------

    def search(self, identity: Identity, query: str, limit: int, stats: PipelineStats) -> SearchOutcome:
        query = self._clean_input(query, "query", self.policy.config.max_input_chars)
        found = self.store.search(identity.tenant_id, identity.user_id, query, SEARCH_CANDIDATES)
        keyword_hits = [m for m in found if not is_expired(m)]
        by_id = {m.id: m for m in keyword_hits}
        for memory in self.store.list_all(identity.tenant_id, identity.user_id, SEARCH_RECENT_LIMIT):
            if not is_expired(memory):
                by_id.setdefault(memory.id, memory)
        candidates = list(by_id.values())  # keyword hits first, then recent memories
        if not candidates:
            stats.selection = "none"
            return SearchOutcome([], "none")

        refs = {f"m{i}": memory for i, memory in enumerate(candidates, start=1)}
        try:
            redacted = self.redactor.redact(query)
            stats.dlp_findings += redacted.findings
            payload = {
                "query": redacted.text,
                "limit": limit,
                "memories": [{"ref": ref, "fact": m.fact} for ref, m in refs.items()],
            }
            result = self._generate(
                "relevance", wrap(json.dumps(payload, ensure_ascii=False)), RelevanceResult, stats
            )
        except (RedactorError, LlmError) as exc:
            # Reads may degrade: they only ever touch this user's own memories.
            # Only keyword hits: without the model, recent memories are not known to be relevant.
            logger.warning("search_memory fell back to store order: %s", type(exc).__name__)
            stats.selection = "fallback"
            return SearchOutcome(keyword_hits[:limit], "fallback")

        chosen: list[Memory] = []
        for ref in result.refs:
            memory = refs.get(ref)  # unknown refs are dropped
            if memory is not None and memory not in chosen:
                chosen.append(memory)
        stats.selection = "gemini"
        return SearchOutcome(chosen[:limit], "gemini")
