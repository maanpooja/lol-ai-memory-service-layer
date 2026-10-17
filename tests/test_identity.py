import pytest

from memory_mcp.identity import (
    DevIdentityProvider,
    Identity,
    current_identity,
    get_identity,
    get_identity_provider,
)

DEFAULT = Identity("dev-tenant", "dev-user", "dev-app")
HEADERS = {"x-dev-tenant": "t2", "x-dev-user": "u2", "x-dev-app": "a2"}
DEV_VARS = ("AUTH_MODE", "DEV_TENANT_ID", "DEV_USER_ID", "DEV_APP_ID", "DEV_ALLOW_HEADER_OVERRIDE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DEV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_get_identity_raises_when_unset() -> None:
    token = current_identity.set(None)
    try:
        with pytest.raises(RuntimeError):
            get_identity()
    finally:
        current_identity.reset(token)


def test_dev_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DevIdentityProvider.from_env("stdio").identify(None) == DEFAULT
    monkeypatch.setenv("DEV_TENANT_ID", "acme")
    monkeypatch.setenv("DEV_USER_ID", "alice")
    monkeypatch.setenv("DEV_APP_ID", "notes")
    assert DevIdentityProvider.from_env("stdio").identify(None) == Identity("acme", "alice", "notes")


def test_headers_are_ignored_by_default() -> None:
    assert DevIdentityProvider.from_env("http").identify(HEADERS) == DEFAULT


def test_header_override_only_on_http_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_ALLOW_HEADER_OVERRIDE", "true")
    http = DevIdentityProvider.from_env("http")
    assert http.identify(HEADERS) == Identity("t2", "u2", "a2")
    assert http.identify({"x-dev-user": "u2"}) == Identity("dev-tenant", "u2", "dev-app")
    assert http.identify({}) == DEFAULT
    stdio = DevIdentityProvider.from_env("stdio")
    assert not stdio.allow_header_override
    assert stdio.identify(HEADERS) == DEFAULT


def test_factory_warns_about_stub_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    get_identity_provider("stdio")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "STUB identity" in captured.err
    assert "DEV_ALLOW_HEADER_OVERRIDE is ON" not in captured.err


def test_factory_warns_loudly_when_override_is_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEV_ALLOW_HEADER_OVERRIDE", "true")
    get_identity_provider("http")
    assert "DEV_ALLOW_HEADER_OVERRIDE is ON" in capsys.readouterr().err


def test_factory_rejects_other_auth_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "jwt")
    with pytest.raises(NotImplementedError):
        get_identity_provider("http")
