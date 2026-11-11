import hashlib
import time
from datetime import datetime

import psycopg
import pytest

from cad2anything.store import pgmirror
from cad2anything.store.pgmirror import PgMirror, mirror_from_env
from cad2anything.store.postgis import TOKEN_REFRESH_MARGIN_S


class MemoryMirror(PgMirror):
    """PgMirror with the three storage primitives backed by a dict."""

    def __init__(self):
        super().__init__(connect=lambda: None)
        self.rows: dict[str, bytes] = {}
        self.upserts: list[str] = []

    def remote_index(self):
        return {name: hashlib.sha256(data).hexdigest() for name, data in self.rows.items()}

    def fetch(self, name):
        return self.rows[name]

    def upsert(self, name, data):
        self.upserts.append(name)
        self.rows[name] = data

    def delete(self, name):
        del self.rows[name]


def test_prune_removes_files_missing_locally(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "R0.json").write_text("{}", encoding="utf-8")
    mirror = MemoryMirror()
    mirror.rows = {"models/R0.json": b"{}", "models/H2.json": b"{}", "other/keep.txt": b"x"}
    assert mirror.prune(tmp_path) == 1
    assert sorted(mirror.rows) == ["models/R0.json", "other/keep.txt"]


def test_push_tree_uploads_only_changed_files_within_prefixes(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "R0.json").write_text("{}", encoding="utf-8")
    (tmp_path / "changes").mkdir()
    (tmp_path / "changes" / "R0__R1.json").write_text("[]", encoding="utf-8")
    (tmp_path / "scratch.txt").write_text("ignored", encoding="utf-8")
    mirror = MemoryMirror()

    assert mirror.push_tree(tmp_path) == 2
    assert sorted(mirror.rows) == ["changes/R0__R1.json", "models/R0.json"]
    assert mirror.push_tree(tmp_path) == 0

    (tmp_path / "models" / "R0.json").write_text('{"v": 2}', encoding="utf-8")
    assert mirror.push_tree(tmp_path) == 1
    assert mirror.upserts[-1] == "models/R0.json"


def test_pull_writes_missing_and_stale_files(tmp_path):
    mirror = MemoryMirror()
    mirror.rows = {"models/R1.json": b'{"rev": "R1"}', "reports/a.json": b"[]", "other/x": b"skip"}
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a.json").write_bytes(b"[]")

    assert mirror.pull(tmp_path) == 1
    assert (tmp_path / "models" / "R1.json").read_bytes() == b'{"rev": "R1"}'
    assert not (tmp_path / "other").exists()

    (tmp_path / "models" / "R1.json").write_bytes(b"stale")
    assert mirror.pull(tmp_path) == 1
    assert mirror.pull(tmp_path) == 0


def test_mirror_selection_from_environment(monkeypatch):
    for name in ("WORKSPACE_MIRROR", "POSTGRES_HOST", "PGHOST", "DATA_STORAGE_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    assert mirror_from_env() is None

    monkeypatch.setenv("WORKSPACE_MIRROR", "postgres")
    with pytest.raises(RuntimeError):
        mirror_from_env()
    monkeypatch.setenv("POSTGRES_HOST", "example.invalid")
    assert isinstance(mirror_from_env(), pgmirror.PgMirror)

    monkeypatch.setenv("WORKSPACE_MIRROR", "none")
    assert mirror_from_env() is None
    monkeypatch.setenv("WORKSPACE_MIRROR", "blob")
    with pytest.raises(RuntimeError):
        mirror_from_env()
    monkeypatch.setenv("WORKSPACE_MIRROR", "ftp")
    with pytest.raises(RuntimeError):
        mirror_from_env()


class FakeConnection:
    def __init__(self, failure=None):
        self.failure = failure
        self.closed = False
        self.autocommit = False
        self.statements: list[str] = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.failure is not None and sql.startswith("SELECT"):
            failure, self.failure = self.failure, None
            raise failure

    def fetchall(self):
        return [("models/R0.json", "sha")]

    def close(self):
        self.closed = True


@pytest.mark.parametrize("error", [psycopg.OperationalError, psycopg.InterfaceError])
def test_reconnects_after_half_open_connection_failure(error):
    first = FakeConnection(error("connection lost"))
    second = FakeConnection()
    connections = iter((first, second))
    mirror = PgMirror(connect=lambda: next(connections))

    assert mirror.remote_index() == {"models/R0.json": "sha"}
    assert first.closed
    assert first.statements[0] == second.statements[0] == pgmirror.TABLE_SQL
    assert mirror.error_count == 1
    assert mirror.health()["db"] == "ok"


def test_retries_only_once_then_records_error_and_recovery():
    first = FakeConnection(psycopg.OperationalError("lost"))
    second = FakeConnection(psycopg.InterfaceError("lost again"))
    recovered = FakeConnection()
    connections = iter((first, second, recovered))
    mirror = PgMirror(connect=lambda: next(connections))

    with pytest.raises(psycopg.InterfaceError):
        mirror.remote_index()
    assert first.closed and second.closed
    assert mirror.health()["db"] == "down"
    assert mirror.health()["mirror_errors"] == 2
    assert mirror.health()["last_mirror_error"] == "InterfaceError"
    assert datetime.fromisoformat(mirror.health()["last_mirror_error_at"]).tzinfo is not None
    assert mirror.healthcheck()
    assert mirror.health()["db"] == "ok"
    assert mirror.health()["mirror_errors"] == 2


def test_token_refreshes_before_expiry():
    first = FakeConnection()
    second = FakeConnection()
    connections = iter((first, second))
    mirror = PgMirror(connect=lambda: next(connections))

    mirror.remote_index()
    first._cad2anything_token_expires_at = time.time() + TOKEN_REFRESH_MARGIN_S - 1
    assert mirror.remote_index() == {"models/R0.json": "sha"}
    assert first.closed
    assert len(second.statements) == 2  # schema and query are re-run on the fresh connection


def test_healthcheck_detects_half_open_connection_and_recovers():
    first = FakeConnection()
    second = FakeConnection(psycopg.OperationalError("still offline"))
    third = FakeConnection()
    connections = iter((first, second, third))
    mirror = PgMirror(connect=lambda: next(connections))

    mirror.remote_index()
    first.failure = psycopg.InterfaceError("half open")
    assert mirror.healthcheck() is False
    assert mirror.health()["db"] == "down"
    assert mirror.health()["mirror_errors"] == 2
    assert mirror.healthcheck() is True
    assert mirror.health()["db"] == "ok"


def test_lock_wait_is_bounded_and_reported(monkeypatch):
    mirror = PgMirror(connect=lambda: FakeConnection())
    monkeypatch.setattr(pgmirror, "LOCK_TIMEOUT_S", 0.01)
    mirror._lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            mirror.remote_index()
    finally:
        mirror._lock.release()
    assert mirror.health()["mirror_errors"] == 1
    assert mirror.health()["db"] == "down"
    assert mirror.health()["last_mirror_error"] == "TimeoutError"


def test_push_tree_skips_in_progress_atomic_files(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / ".R0.json.partial.tmp").write_bytes(b"partial")
    (model_dir / "R0.json").write_bytes(b"complete")
    mirror = MemoryMirror()

    assert mirror.push_tree(tmp_path) == 1
    assert mirror.rows == {"models/R0.json": b"complete"}
