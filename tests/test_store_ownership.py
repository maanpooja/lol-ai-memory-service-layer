"""Ownership and filter enforcement, using in-memory fakes instead of GCP."""

from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from google.cloud import discoveryengine_v1 as de

from memory_mcp.config import Config
from memory_mcp.store.discovery_engine import DiscoveryEngineStore

A = ("tenant-1", "alice")
B = ("tenant-1", "bob")
A_OTHER_TENANT = ("tenant-2", "alice")


class FakeDocuments:
    def __init__(self) -> None:
        self.docs: dict[str, de.Document] = {}

    def create_document(self, parent: str, document: de.Document, document_id: str) -> None:
        name = f"{parent}/documents/{document_id}"
        self.docs[name] = de.Document(name=name, id=document_id, struct_data=dict(document.struct_data))

    def get_document(self, name: str) -> de.Document:
        if name not in self.docs:
            raise gexc.NotFound("missing")
        return self.docs[name]

    def update_document(self, request: de.UpdateDocumentRequest) -> None:
        doc = request.document
        self.docs[doc.name] = de.Document(
            name=doc.name, id=doc.name.rsplit("/", 1)[-1], struct_data=dict(doc.struct_data)
        )

    def delete_document(self, name: str) -> None:
        del self.docs[name]

    def list_documents(self, request: de.ListDocumentsRequest) -> list[de.Document]:
        return list(self.docs.values())


class LeakySearch:
    """Ignores the filter and returns every document, to test the client-side check."""

    def __init__(self, documents: FakeDocuments) -> None:
        self.documents = documents
        self.requests: list[de.SearchRequest] = []

    def search(self, request: de.SearchRequest) -> list[SimpleNamespace]:
        self.requests.append(request)
        return [SimpleNamespace(id=d.id, document=d) for d in self.documents.docs.values()]


@pytest.fixture
def store() -> DiscoveryEngineStore:
    documents = FakeDocuments()
    return DiscoveryEngineStore(Config(project_id="p"), documents=documents, search=LeakySearch(documents))


def test_owner_round_trip(store: DiscoveryEngineStore) -> None:
    created = store.create(*A, "fact", "preference", 0.9, "tests")
    assert store.get(*A, created.id) == created


@pytest.mark.parametrize("intruder", [B, A_OTHER_TENANT])
def test_other_users_cannot_touch_a_memory(store: DiscoveryEngineStore, intruder: tuple[str, str]) -> None:
    created = store.create(*A, "fact", "preference", 0.9, "tests")
    assert store.get(*intruder, created.id) is None
    with pytest.raises(PermissionError):
        store.update(*intruder, created.id, "hijacked")
    assert store.delete(*intruder, created.id) is False
    assert store.get(*A, created.id) == created


def test_missing_and_foreign_memories_look_the_same(store: DiscoveryEngineStore) -> None:
    created = store.create(*A, "fact", "preference", 0.9, "tests")
    missing = "3f2b8c1e-9d4a-4f6b-8a1c-2e5d7f9b0c3a"
    errors = []
    for memory_id in (created.id, missing):
        with pytest.raises(PermissionError) as excinfo:
            store.update(*B, memory_id, "x")
        errors.append(str(excinfo.value).replace(memory_id, "ID"))
    assert errors[0] == errors[1]


def test_non_uuid_memory_id_is_never_sent_to_the_api(store: DiscoveryEngineStore) -> None:
    assert store.get(*A, "../../other/documents/x") is None
    assert store.delete(*A, "not-a-uuid") is False


def test_update_changes_fact_and_bumps_updated_at(store: DiscoveryEngineStore) -> None:
    created = store.create(*A, "old", "preference", 0.9, "tests")
    updated = store.update(*A, created.id, "new")
    assert updated.fact == "new"
    assert updated.updated_at > created.updated_at
    assert updated.created_at == created.created_at
    assert store.get(*A, created.id) == updated


def test_delete_removes_the_memory(store: DiscoveryEngineStore) -> None:
    created = store.create(*A, "fact", "preference", 0.9, "tests")
    assert store.delete(*A, created.id) is True
    assert store.get(*A, created.id) is None
    assert store.delete(*A, created.id) is False


def test_search_and_list_send_the_owner_filter_and_drop_foreign_results(
    store: DiscoveryEngineStore,
) -> None:
    mine = store.create(*A, "mine", "preference", 0.9, "tests")
    store.create(*B, "bobs", "preference", 0.9, "tests")
    store.create(*A_OTHER_TENANT, "other tenant", "preference", 0.9, "tests")

    assert [m.id for m in store.search(*A, "anything")] == [mine.id]
    assert [m.id for m in store.list_all(*A)] == [mine.id]
    assert [m.id for m in store.list_all_scan(*A)] == [mine.id]

    expected = 'tenant_id: ANY("tenant-1") AND user_id: ANY("alice")'
    assert [r.filter for r in store._search.requests] == [expected, expected]


def test_search_rejects_empty_query_and_bad_ids(store: DiscoveryEngineStore) -> None:
    with pytest.raises(ValueError):
        store.search(*A, "  ")
    with pytest.raises(ValueError):
        store.search("tenant-1", 'x") OR user_id: ANY("bob', "q")
    with pytest.raises(ValueError):
        store.list_all("", "alice")
    with pytest.raises(ValueError):
        store.create("tenant-1", "", "fact", "preference", 0.9, "tests")
