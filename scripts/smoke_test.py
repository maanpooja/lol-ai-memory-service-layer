"""End-to-end storage checks against the real data store. Synthetic data only.

Usage:
    uv run python scripts/smoke_test.py

Exits non-zero if any check fails. All test documents are deleted at the end.
"""

import sys
import time
from collections.abc import Callable
from pathlib import Path

# Make src/ importable without relying on the editable install's .pth file,
# which Python 3.12 skips when macOS marks it hidden (e.g. under ~/Desktop).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google.auth.exceptions import DefaultCredentialsError

from memory_mcp.config import load_config
from memory_mcp.models import Memory
from memory_mcp.store.discovery_engine import DiscoveryEngineStore

POLL_SECONDS = 2
TIMEOUT_SECONDS = 120

Owner = tuple[str, str]  # (tenant_id, user_id)

ALICE: Owner = ("smoke-tenant-1", "smoke-alice")
BOB: Owner = ("smoke-tenant-1", "smoke-bob")  # same tenant, other user
ALICE_T2: Owner = ("smoke-tenant-2", "smoke-alice")  # same user id, other tenant
CAROL: Owner = ("smoke-tenant-2", "smoke-carol")
INTRUDERS: dict[str, Owner] = {"bob": BOB, "alice@tenant-2": ALICE_T2, "carol": CAROL}

ALICE_FACT = "Synthetic: prefers window seats on long flights"
ALICE_QUERY = "window seats"


class CheckFailed(Exception):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def wait_until(predicate: Callable[[], bool]) -> float | None:
    """Poll until the predicate is true. Returns seconds elapsed, or None on timeout."""
    start = time.monotonic()
    while True:
        if predicate():
            return time.monotonic() - start
        if time.monotonic() - start >= TIMEOUT_SECONDS:
            return None
        time.sleep(POLL_SECONDS)


def fmt(seconds: float | None) -> str:
    return f">{TIMEOUT_SECONDS}s (timed out)" if seconds is None else f"{seconds:.1f}s"


def ids(memories: list[Memory]) -> set[str]:
    return {m.id for m in memories}


class SmokeTest:
    def __init__(self, store: DiscoveryEngineStore) -> None:
        self.store = store
        self.created: list[tuple[Owner, str]] = []
        self.results: list[tuple[str, str, str]] = []
        self.alice_memory: Memory | None = None

    def create(self, owner: Owner, fact: str, category: str = "preference") -> Memory:
        memory = self.store.create(*owner, fact, category, 0.9, "smoke-test")
        self.created.append((owner, memory.id))
        return memory

    def run(self, name: str, check: Callable[[], str]) -> None:
        print(f"-> {name} ...", flush=True)
        try:
            detail, status = check(), "PASS"
        except CheckFailed as exc:
            detail, status = str(exc), "FAIL"
        except Exception as exc:  # report and carry on to the other checks
            detail, status = f"{type(exc).__name__}: {exc}", "FAIL"
        self.results.append((name, status, detail))
        print(f"   {status}: {detail}", flush=True)

    def alice(self) -> Memory:
        if self.alice_memory is None:
            raise CheckFailed("skipped: check (a) did not create Alice's memory")
        return self.alice_memory

    # --- checks -----------------------------------------------------------

    def check_create_get(self) -> str:
        created = self.create(ALICE, ALICE_FACT)
        fetched = self.store.get(*ALICE, created.id)
        expect(fetched is not None, "get returned None right after create")
        expect(fetched == created, "fetched memory differs from the created one")
        self.alice_memory = created
        return f"round-trip ok id={created.id}"

    def check_latency(self) -> str:
        memory = self.create(ALICE, "Synthetic: enjoys hiking in the mountains", "hobby")
        get_s = wait_until(lambda: self.store.get(*ALICE, memory.id) is not None)
        scan_s = wait_until(lambda: memory.id in ids(self.store.list_all_scan(*ALICE)))
        search_s = wait_until(lambda: memory.id in ids(self.store.search(*ALICE, "hiking mountains")))
        list_s = wait_until(lambda: memory.id in ids(self.store.list_all(*ALICE)))
        detail = (
            f"get={fmt(get_s)} list_all_scan={fmt(scan_s)} "
            f"search={fmt(search_s)} list_all={fmt(list_s)}"
        )
        expect(get_s is not None, f"get never returned the new memory ({detail})")
        expect(search_s is not None, f"search never returned the new memory ({detail})")
        return detail

    def check_search(self) -> str:
        target = self.alice()
        bob_memory = self.create(BOB, "Synthetic: prefers window seats and aisle snacks")
        elapsed = wait_until(lambda: target.id in ids(self.store.search(*ALICE, ALICE_QUERY)))
        expect(elapsed is not None, "Alice's memory never became searchable")
        found = self.store.search(*ALICE, ALICE_QUERY)
        expect(bob_memory.id not in ids(found), "Alice's search returned Bob's memory")
        expect(all(m.tenant_id == ALICE[0] and m.user_id == ALICE[1] for m in found),
               "search returned a memory owned by someone else")
        return f"query matched the right user's memory ({len(found)} result(s))"

    def check_isolation(self) -> str:
        target = self.alice()
        # The search/list checks only mean something once the memory is indexed.
        indexed = wait_until(lambda: target.id in ids(self.store.search(*ALICE, ALICE_QUERY)))
        expect(indexed is not None, "cannot test search isolation: memory never became searchable")
        for label, intruder in INTRUDERS.items():
            self.create(intruder, f"Synthetic: {label} likes window seats too")
            expect(target.id not in ids(self.store.search(*intruder, ALICE_QUERY)),
                   f"LEAK: {label} found the memory through search")
            expect(target.id not in ids(self.store.list_all(*intruder)),
                   f"LEAK: {label} found the memory through list_all")
            expect(target.id not in ids(self.store.list_all_scan(*intruder)),
                   f"LEAK: {label} found the memory through list_all_scan")
            expect(self.store.get(*intruder, target.id) is None,
                   f"LEAK: {label} fetched the memory through get")
            try:
                self.store.update(*intruder, target.id, "Synthetic: overwritten by intruder")
            except PermissionError:
                pass
            else:
                raise CheckFailed(f"LEAK: {label} updated the memory")
            expect(self.store.delete(*intruder, target.id) is False,
                   f"LEAK: {label} deleted the memory")
        survivor = self.store.get(*ALICE, target.id)
        expect(survivor is not None, "memory was gone after the intruder attempts")
        expect(survivor.fact == target.fact, "memory was modified by an intruder")
        return f"{len(INTRUDERS)} intruders blocked on search, list, get, update, delete"

    def check_update(self) -> str:
        target = self.alice()
        new_fact = "Synthetic: now prefers aisle seats"
        updated = self.store.update(*ALICE, target.id, new_fact)
        fetched = self.store.get(*ALICE, target.id)
        expect(fetched is not None, "get returned None after update")
        expect(fetched.fact == new_fact, "fact was not changed")
        expect(fetched.updated_at > target.updated_at, "updated_at was not bumped")
        expect(fetched.created_at == target.created_at, "created_at changed")
        expect(updated == fetched, "update() return value differs from the stored memory")
        return f"fact changed, updated_at {target.updated_at} -> {fetched.updated_at}"

    def check_list(self) -> str:
        expected = {mid for owner, mid in self.created if owner == ALICE}
        scan_ok = expected <= ids(self.store.list_all_scan(*ALICE))
        try:
            search_s = wait_until(lambda: expected <= ids(self.store.list_all(*ALICE)))
            search_note = f"empty-query search listing ok after {fmt(search_s)}" if search_s is not None \
                else "empty-query search listing never returned all memories"
        except Exception as exc:
            search_s, search_note = None, f"empty-query search listing errored ({type(exc).__name__}: {exc})"
        detail = f"{search_note}; documents.list scan {'ok' if scan_ok else 'MISSING memories'}"
        expect(search_s is not None, f"{detail}. Switch list_all to list_all_scan.")
        expect(scan_ok, detail)
        return detail

    def check_delete(self) -> str:
        target = self.alice()
        expect(self.store.delete(*ALICE, target.id) is True, "delete returned False")
        expect(self.store.get(*ALICE, target.id) is None, "get still returns the memory")
        expect(target.id not in ids(self.store.list_all_scan(*ALICE)), "scan still lists the memory")
        search_s = wait_until(lambda: target.id not in ids(self.store.search(*ALICE, "seats")))
        list_s = wait_until(lambda: target.id not in ids(self.store.list_all(*ALICE)))
        detail = f"gone from get immediately, search after {fmt(search_s)}, list_all after {fmt(list_s)}"
        expect(search_s is not None and list_s is not None, f"still visible: {detail}")
        return detail

    # --- teardown and report ----------------------------------------------

    def cleanup(self) -> None:
        print("\ncleaning up test documents...")
        failed = 0
        for owner, memory_id in self.created:
            try:
                self.store.delete(*owner, memory_id)
            except Exception as exc:
                failed += 1
                print(f"   could not delete {memory_id}: {type(exc).__name__}")
        print(f"   attempted {len(self.created)}, {failed} failed")

    def report(self) -> bool:
        width = max(len(name) for name, _, _ in self.results)
        print("\n" + "=" * 72)
        for name, status, detail in self.results:
            print(f"{name.ljust(width)}  {status}  {detail}")
        print("=" * 72)
        failures = sum(1 for _, status, _ in self.results if status == "FAIL")
        print(f"{len(self.results) - failures} passed, {failures} failed")
        return failures == 0


def main() -> int:
    config = load_config()
    print(f"project={config.project_id} data_store={config.data_store_id}")
    print(f"serving config: {config.serving_config_path}\n")
    try:
        store = DiscoveryEngineStore(config)
    except DefaultCredentialsError:
        print(
            "ERROR: no Google credentials found. Run:\n"
            "  gcloud auth application-default login\n"
            f"  gcloud auth application-default set-quota-project {config.project_id}",
            file=sys.stderr,
        )
        return 1
    test = SmokeTest(store)
    try:
        test.run("a. create + get round-trip", test.check_create_get)
        test.run("f. indexing latency", test.check_latency)
        test.run("b. search returns right user", test.check_search)
        test.run("c. isolation", test.check_isolation)
        test.run("d. update", test.check_update)
        test.run("g. list_all", test.check_list)
        test.run("e. delete", test.check_delete)
    finally:
        test.cleanup()
    return 0 if test.report() else 1


if __name__ == "__main__":
    sys.exit(main())
