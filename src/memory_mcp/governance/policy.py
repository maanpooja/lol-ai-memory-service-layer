"""Deterministic rules applied in code. The model proposes; this module decides.

Nothing here calls a network service, so every rule is unit-testable.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from memory_mcp.config import CATEGORIES, PolicyConfig
from memory_mcp.models import Memory

MAX_FACT_CHARS = 500

# A redaction token such as [EMAIL_ADDRESS] or [PERSON_NAME].
_PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_]{2,}\]")
_SPACE_RE = re.compile(r"\s+")


class Verdict(str, Enum):
    KEEP = "keep"
    SENSITIVE = "sensitive"  # counted in sensitive_not_saved
    REJECTED = "rejected"  # counted in rejected


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text))


def normalize(text: str) -> str:
    """Comparison key for de-duplication: case, spacing and end punctuation ignored."""
    return _SPACE_RE.sub(" ", text).strip().rstrip(".!").strip().lower()


def is_sensitive_label(sensitivity: str) -> bool:
    """Anything other than an explicit "none" counts as sensitive, so unknown labels fail safe."""
    return sensitivity.strip().lower() != "none"


@dataclass(frozen=True)
class Policy:
    config: PolicyConfig

    def judge(
        self, fact: str, category: str, confidence: float, sensitivity: str, authority_claim: bool
    ) -> Verdict:
        """Rule order matters only for which counter a dropped fact lands in."""
        if is_sensitive_label(sensitivity) or category in self.config.blocked_categories:
            return Verdict.SENSITIVE
        if has_placeholder(fact):
            return Verdict.SENSITIVE  # data minimisation: no redacted PII shells
        if authority_claim:
            return Verdict.REJECTED
        if category not in CATEGORIES:
            return Verdict.REJECTED
        if not 0.0 <= confidence <= 1.0 or confidence < self.config.min_confidence:
            return Verdict.REJECTED
        if not fact.strip() or len(fact) > MAX_FACT_CHARS:
            return Verdict.REJECTED
        return Verdict.KEEP

    def expires_at(self, category: str, now: datetime | None = None) -> str | None:
        days = self.config.ttl_days.get(category)
        if days is None or days <= 0:
            return None
        moment = (now or datetime.now(timezone.utc)) + timedelta(days=days)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def is_expired(memory: Memory, now: datetime | None = None) -> bool:
    if not memory.expires_at:
        return False
    try:
        expires = datetime.fromisoformat(memory.expires_at)
    except ValueError:
        return False  # an unreadable expiry never hides a memory
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= (now or datetime.now(timezone.utc))
