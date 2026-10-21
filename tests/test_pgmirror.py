import hashlib

import pytest

from cad2anything.store import pgmirror
from cad2anything.store.pgmirror import PgMirror, mirror_from_env


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
