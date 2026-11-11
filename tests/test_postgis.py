from types import SimpleNamespace

import psycopg

from cad2anything.store import postgis


def test_connect_bounds_network_and_statement_waits(monkeypatch):
    captured = {}
    connection = SimpleNamespace()
    monkeypatch.setattr(
        postgis,
        "connection_settings",
        lambda: {"host": "db.example.invalid", "dbname": "test", "user": "test-user"},
    )
    monkeypatch.setenv("PGPASSWORD", "local-test-password")
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: captured.update(kwargs) or connection)

    assert postgis.connect() is connection
    assert captured["sslmode"] == "require"
    assert captured["connect_timeout"] == postgis.CONNECT_TIMEOUT_S
    assert f"statement_timeout={postgis.STATEMENT_TIMEOUT_MS}" in captured["options"]
    assert (captured["keepalives"], captured["keepalives_idle"], captured["keepalives_interval"]) == (
        1,
        30,
        10,
    )
    assert captured["keepalives_count"] == 3
    assert not hasattr(connection, "_cad2anything_token_expires_at")


def test_entra_expiry_is_attached_to_connection(monkeypatch):
    captured = {}
    connection = SimpleNamespace()
    expires_on = 1_800_000_000
    monkeypatch.setattr(
        postgis,
        "connection_settings",
        lambda: {"host": "db.example.invalid", "dbname": "test", "user": "test-user"},
    )
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setattr(
        postgis, "_entra_access_token", lambda: SimpleNamespace(token="synthetic", expires_on=expires_on)
    )
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: captured.update(kwargs) or connection)

    assert postgis.connect() is connection
    assert captured["password"] == "synthetic"
    assert connection._cad2anything_token_expires_at == expires_on
