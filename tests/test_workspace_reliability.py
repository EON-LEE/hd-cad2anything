import json

import pytest

from cad2anything.model import ChangeSet, Revision, SpatialModel
from cad2anything.workspace import Workspace


class RecordingMirror:
    backend = "blob"

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.fail_names: set[str] = set()
        self.calls: list[str] = []

    def push(self, path, root):
        name = path.relative_to(root).as_posix()
        self.calls.append(name)
        if name in self.fail_names:
            raise OSError("synthetic mirror outage")
        self.files[name] = path.read_bytes()

    def delete(self, name):
        self.calls.append(f"delete:{name}")
        if name in self.fail_names:
            raise OSError("synthetic mirror outage")
        self.files.pop(name, None)


def test_model_change_and_report_are_committed_before_mirroring(tmp_path):
    mirror = RecordingMirror()
    ws = Workspace(tmp_path, mirror=mirror)
    paths = (
        ws.save_model(SpatialModel(Revision("R1")), {"source": "synthetic"}),
        ws.save_change_set(ChangeSet("R0", "R1")),
        ws.save_report("R1", {"ok": True}),
    )
    for path in paths:
        name = path.relative_to(tmp_path).as_posix()
        assert json.loads(path.read_bytes()) and mirror.files[name] == path.read_bytes()
    assert not list(tmp_path.rglob(".*.tmp"))


def test_atomic_replace_failure_preserves_previous_file_without_publishing(tmp_path, monkeypatch):
    mirror = RecordingMirror()
    ws = Workspace(tmp_path, mirror=mirror)
    path = ws.write_bytes("business/metadata.csv", b"before")
    previous_calls = len(mirror.calls)

    def fail_replace(*_):
        raise OSError("synthetic local write failure")

    monkeypatch.setattr("cad2anything.workspace.os.replace", fail_replace)
    with pytest.raises(OSError):
        ws.write_bytes("business/metadata.csv", b"after")

    assert path.read_bytes() == b"before"
    assert len(mirror.calls) == previous_calls
    assert not list(path.parent.glob(".*.tmp"))


def test_failed_publish_preserves_local_commit_and_retries_after_recovery(tmp_path):
    mirror = RecordingMirror()
    ws = Workspace(tmp_path, mirror=mirror)
    mirror.fail_names.add("reports/R1.json")

    with pytest.raises(OSError):
        ws.write_json("reports/R1.json", {"ok": True})

    assert ws.read_json("reports/R1.json") == {"ok": True}
    assert ws.mirror_publish_errors == 1
    assert ws.mirror_status()["mirror_errors"] == 1
    assert ws.mirror_status()["writes_available"] is False
    assert ws.mirror_status()["last_mirror_error"] == "OSError"
    assert ws.mirror_status()["last_mirror_error_at"]
    assert ws.mirror_status()["pending_mirror_writes"] == 1

    mirror.fail_names.clear()
    assert ws.retry_pending_mirror() == 1
    assert json.loads(mirror.files["reports/R1.json"]) == {"ok": True}
    assert ws.mirror_status()["writes_available"] is True
    assert ws.mirror_status()["pending_mirror_writes"] == 0


def test_publish_tree_attempts_all_files_and_tracks_partial_failure(tmp_path):
    mirror = RecordingMirror()
    ws = Workspace(tmp_path, mirror=mirror)
    ws.write_text("artifacts/R1/one.svg", "<svg>1</svg>", publish=False)
    ws.write_text("artifacts/R1/two.svg", "<svg>2</svg>", publish=False)
    mirror.fail_names.add("artifacts/R1/one.svg")

    with pytest.raises(RuntimeError, match="1 workspace files"):
        ws.publish_tree(ws.artifacts_dir / "R1")

    assert mirror.files["artifacts/R1/two.svg"] == b"<svg>2</svg>"
    assert ws.mirror_status()["mirror_errors"] == 1
    assert ws.mirror_status()["pending_mirror_writes"] == 1

    mirror.fail_names.clear()
    assert ws.retry_pending_mirror() == 1
    assert mirror.files["artifacts/R1/one.svg"] == b"<svg>1</svg>"


def test_publish_tree_handles_resolved_path_with_relative_workspace_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mirror = RecordingMirror()
    ws = Workspace("workspace", mirror=mirror)
    ws.write_text("artifacts/R1/one.svg", "<svg/>", publish=False)

    ws.publish_tree((ws.artifacts_dir / "R1").resolve())
    assert mirror.files == {"artifacts/R1/one.svg": b"<svg/>"}


def test_failed_mirror_delete_is_replayed_after_recovery(tmp_path):
    mirror = RecordingMirror()
    ws = Workspace(tmp_path, mirror=mirror)
    ws.write_text("reports/R1.json", "{}")
    mirror.fail_names.add("reports/R1.json")

    with pytest.raises(OSError):
        ws.delete_file("reports/R1.json")

    assert not (tmp_path / "reports" / "R1.json").exists()
    assert ws.mirror_status()["pending_mirror_writes"] == 1
    mirror.fail_names.clear()
    assert ws.retry_pending_mirror() == 1
    assert "reports/R1.json" not in mirror.files
