"""The Memory record. One Memory is one Discovery Engine document."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string, e.g. 2026-01-31T12:00:00.123456Z."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Memory:
    id: str
    tenant_id: str
    user_id: str
    fact: str
    category: str
    confidence: float
    source_app: str
    created_at: str
    updated_at: str
    expires_at: str | None = None

    def to_struct(self) -> dict[str, Any]:
        """Fields stored in the document's struct_data (the id is the document id)."""
        data: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "fact": self.fact,
            "category": self.category,
            "confidence": self.confidence,
            "source_app": self.source_app,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        # A null in a datetime field is not a valid value, so leave the key out.
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at
        return data

    @classmethod
    def from_struct(cls, memory_id: str, data: Mapping[str, Any]) -> "Memory":
        return cls(
            id=memory_id,
            tenant_id=str(data["tenant_id"]),
            user_id=str(data["user_id"]),
            fact=str(data.get("fact", "")),
            category=str(data.get("category", "")),
            confidence=float(data.get("confidence", 0.0)),
            source_app=str(data.get("source_app", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            expires_at=str(data["expires_at"]) if data.get("expires_at") else None,
        )
