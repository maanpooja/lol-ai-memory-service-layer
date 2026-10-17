import pytest

from memory_mcp.store.discovery_engine import (
    build_owner_filter,
    escape_literal,
    is_memory_id,
    is_owned_by,
    validate_id,
)


def test_filter_contains_both_ids() -> None:
    assert (
        build_owner_filter("tenant-1", "user-1")
        == 'tenant_id: ANY("tenant-1") AND user_id: ANY("user-1")'
    )


@pytest.mark.parametrize("good", ["a", "user-1", "a.b_c", "alice@example.com", "org:123", "A" * 128])
def test_validate_id_accepts_plain_ids(good: str) -> None:
    assert validate_id(good, "user_id") == good


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "user 1",
        'u") OR tenant_id: ANY("other',
        'a"b',
        "a\\b",
        "a)b",
        "a,b",
        "a\nb",
        "*",
        "-leading-dash",
        "A" * 129,
        "ünï",
        None,
        123,
    ],
)
def test_validate_id_rejects_odd_ids(bad: object) -> None:
    with pytest.raises(ValueError):
        validate_id(bad, "user_id")  # type: ignore[arg-type]


@pytest.mark.parametrize("tenant,user", [("", "u"), ("t", ""), ('t"', "u"), ("t", 'u") OR x: ANY("y')])
def test_filter_rejects_bad_ids(tenant: str, user: str) -> None:
    with pytest.raises(ValueError):
        build_owner_filter(tenant, user)


def test_error_message_does_not_echo_the_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_id('secret"value', "tenant_id")
    assert "secret" not in str(excinfo.value)


def test_escape_literal_escapes_quotes_and_backslashes() -> None:
    assert escape_literal('a"b') == 'a\\"b'
    assert escape_literal("a\\b") == "a\\\\b"
    # Backslash is escaped first, so an escaped quote cannot be un-escaped.
    assert escape_literal('\\"') == '\\\\\\"'
    assert escape_literal("plain") == "plain"


def test_is_owned_by_requires_both_ids() -> None:
    data = {"tenant_id": "t1", "user_id": "u1"}
    assert is_owned_by(data, "t1", "u1")
    assert not is_owned_by(data, "t2", "u1")
    assert not is_owned_by(data, "t1", "u2")
    assert not is_owned_by({}, "t1", "u1")


def test_is_memory_id_only_accepts_canonical_uuids() -> None:
    assert is_memory_id("3f2b8c1e-9d4a-4f6b-8a1c-2e5d7f9b0c3a")
    for bad in ["", "abc", "../other", "3F2B8C1E-9D4A-4F6B-8A1C-2E5D7F9B0C3A",
                "{3f2b8c1e-9d4a-4f6b-8a1c-2e5d7f9b0c3a}", "3f2b8c1e9d4a4f6b8a1c2e5d7f9b0c3a"]:
        assert not is_memory_id(bad)
