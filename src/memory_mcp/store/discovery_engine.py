"""MemoryStore backed by a Vertex AI Search (Discovery Engine) structured data store."""

import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from google.api_core import exceptions as gexc
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as de

from memory_mcp.config import Config
from memory_mcp.models import Memory, utc_now_iso
from memory_mcp.store.base import MemoryStore, StoreError

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}")


def validate_id(value: str, name: str) -> str:
    """Accept only plain ids: 1-128 chars of letters, digits and _ . @ : -"""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def escape_literal(value: str) -> str:
    """Escape a string for use inside a double-quoted filter literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_owner_filter(tenant_id: str, user_id: str) -> str:
    """The filter applied to every search and list. Both ids are always present."""
    tenant = escape_literal(validate_id(tenant_id, "tenant_id"))
    user = escape_literal(validate_id(user_id, "user_id"))
    return f'tenant_id: ANY("{tenant}") AND user_id: ANY("{user}")'


def is_owned_by(data: Mapping[str, Any], tenant_id: str, user_id: str) -> bool:
    return data.get("tenant_id") == tenant_id and data.get("user_id") == user_id


def is_memory_id(value: str) -> bool:
    """True only for a canonical uuid, so an id can never alter a resource path."""
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def client_options(config: Config) -> ClientOptions:
    endpoint = (
        None
        if config.location == "global"
        else f"{config.location}-discoveryengine.googleapis.com"
    )
    return ClientOptions(api_endpoint=endpoint, quota_project_id=config.project_id)


class DiscoveryEngineStore(MemoryStore):
    def __init__(
        self,
        config: Config,
        documents: de.DocumentServiceClient | None = None,
        search: de.SearchServiceClient | None = None,
    ) -> None:
        self._config = config
        options = client_options(config)
        self._documents = documents or de.DocumentServiceClient(client_options=options)
        self._search = search or de.SearchServiceClient(client_options=options)

    def _doc_name(self, memory_id: str) -> str:
        return f"{self._config.branch_path}/documents/{memory_id}"

    def _fetch_owned(self, tenant_id: str, user_id: str, memory_id: str) -> de.Document | None:
        """Fetch a document only if it exists and belongs to this tenant and user."""
        validate_id(tenant_id, "tenant_id")
        validate_id(user_id, "user_id")
        if not is_memory_id(memory_id):
            return None
        try:
            doc = self._documents.get_document(name=self._doc_name(memory_id))
        except gexc.NotFound:
            return None
        except gexc.GoogleAPICallError as exc:
            raise StoreError(f"failed to fetch memory {memory_id}: {type(exc).__name__}") from exc
        if not is_owned_by(doc.struct_data, tenant_id, user_id):
            logger.warning("ownership check failed id=%s tenant=%s user=%s", memory_id, tenant_id, user_id)
            return None
        return doc

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
            tenant_id=validate_id(tenant_id, "tenant_id"),
            user_id=validate_id(user_id, "user_id"),
            fact=fact,
            category=category,
            confidence=float(confidence),
            source_app=source_app,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        try:
            self._documents.create_document(
                parent=self._config.branch_path,
                document=de.Document(struct_data=memory.to_struct()),
                document_id=memory.id,
            )
        except gexc.GoogleAPICallError as exc:
            raise StoreError(f"failed to create memory {memory.id}: {type(exc).__name__}") from exc
        logger.info("created memory id=%s tenant=%s user=%s", memory.id, tenant_id, user_id)
        return memory

    def get(self, tenant_id: str, user_id: str, memory_id: str) -> Memory | None:
        doc = self._fetch_owned(tenant_id, user_id, memory_id)
        if doc is None:
            return None
        return Memory.from_struct(memory_id, doc.struct_data)

    def update(self, tenant_id: str, user_id: str, memory_id: str, fact: str) -> Memory:
        doc = self._fetch_owned(tenant_id, user_id, memory_id)
        if doc is None:
            raise PermissionError(f"memory {memory_id} not found or not owned by caller")
        data = dict(doc.struct_data)
        data["fact"] = fact
        data["updated_at"] = utc_now_iso()
        try:
            self._documents.update_document(
                request=de.UpdateDocumentRequest(
                    document=de.Document(name=doc.name, struct_data=data)
                )
            )
        except gexc.GoogleAPICallError as exc:
            raise StoreError(f"failed to update memory {memory_id}: {type(exc).__name__}") from exc
        logger.info("updated memory id=%s tenant=%s user=%s", memory_id, tenant_id, user_id)
        return Memory.from_struct(memory_id, data)

    def delete(self, tenant_id: str, user_id: str, memory_id: str) -> bool:
        doc = self._fetch_owned(tenant_id, user_id, memory_id)
        if doc is None:
            return False
        try:
            self._documents.delete_document(name=doc.name)
        except gexc.NotFound:
            return False
        except gexc.GoogleAPICallError as exc:
            raise StoreError(f"failed to delete memory {memory_id}: {type(exc).__name__}") from exc
        logger.info("deleted memory id=%s tenant=%s user=%s", memory_id, tenant_id, user_id)
        return True

    def search(self, tenant_id: str, user_id: str, query: str, limit: int = 10) -> list[Memory]:
        if not query.strip():
            raise ValueError("query must not be empty; use list_all to list memories")
        return self._filtered_search(tenant_id, user_id, query, limit)

    def list_all(self, tenant_id: str, user_id: str, limit: int = 100) -> list[Memory]:
        """List via an empty-query search. Subject to indexing latency."""
        return self._filtered_search(tenant_id, user_id, "", limit)

    def list_all_scan(self, tenant_id: str, user_id: str, limit: int = 100) -> list[Memory]:
        """List by scanning documents and filtering here. Fallback for list_all.

        documents.list has no server-side filter, so this reads every tenant's
        documents and costs O(all documents). It does not depend on the index.
        """
        validate_id(tenant_id, "tenant_id")
        validate_id(user_id, "user_id")
        memories: list[Memory] = []
        try:
            request = de.ListDocumentsRequest(
                parent=self._config.branch_path, page_size=MAX_PAGE_SIZE
            )
            for doc in self._documents.list_documents(request=request):
                if is_owned_by(doc.struct_data, tenant_id, user_id):
                    memories.append(Memory.from_struct(doc.id, doc.struct_data))
                    if len(memories) >= limit:
                        break
        except gexc.GoogleAPICallError as exc:
            raise StoreError(f"failed to scan memories: {type(exc).__name__}") from exc
        return memories

    def _filtered_search(self, tenant_id: str, user_id: str, query: str, limit: int) -> list[Memory]:
        if limit < 1:
            return []
        request = de.SearchRequest(
            serving_config=self._config.serving_config_path,
            query=query,
            filter=build_owner_filter(tenant_id, user_id),
            page_size=min(limit, MAX_PAGE_SIZE),
        )
        memories: list[Memory] = []
        try:
            # The pager fetches further pages as we iterate.
            for result in self._search.search(request=request):
                data = result.document.struct_data
                # Defence in depth: never trust the filter alone.
                if not is_owned_by(data, tenant_id, user_id):
                    logger.warning("dropped foreign search result id=%s", result.id)
                    continue
                memories.append(Memory.from_struct(result.document.id or result.id, data))
                if len(memories) >= limit:
                    break
        except gexc.GoogleAPICallError as exc:
            raise StoreError(f"search failed for tenant={tenant_id} user={user_id}: {type(exc).__name__}") from exc
        return memories
