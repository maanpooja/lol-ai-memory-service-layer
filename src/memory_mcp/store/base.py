"""Storage interface. Every method takes tenant_id and user_id explicitly."""

from abc import ABC, abstractmethod

from memory_mcp.models import Memory


class StoreError(RuntimeError):
    """A storage backend call failed. The message never contains memory content."""


class MemoryStore(ABC):
    @abstractmethod
    def create(
        self,
        tenant_id: str,
        user_id: str,
        fact: str,
        category: str,
        confidence: float,
        source_app: str,
        expires_at: str | None = None,
    ) -> Memory: ...

    @abstractmethod
    def get(self, tenant_id: str, user_id: str, memory_id: str) -> Memory | None:
        """Return the memory, or None if it is missing or belongs to someone else."""

    @abstractmethod
    def update(self, tenant_id: str, user_id: str, memory_id: str, fact: str) -> Memory:
        """Replace the fact. Raises PermissionError if missing or not owned by the caller."""

    @abstractmethod
    def delete(self, tenant_id: str, user_id: str, memory_id: str) -> bool:
        """Hard delete. Returns False if missing or not owned by the caller."""

    @abstractmethod
    def search(self, tenant_id: str, user_id: str, query: str, limit: int = 10) -> list[Memory]: ...

    @abstractmethod
    def list_all(self, tenant_id: str, user_id: str, limit: int = 100) -> list[Memory]: ...
