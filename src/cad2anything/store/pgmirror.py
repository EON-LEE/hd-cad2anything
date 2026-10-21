"""Mirror workspace files to a PostgreSQL table using Microsoft Entra authentication.

Use this mirror when Blob Storage is reachable only through private endpoints (for example, when an
organization policy disables public network access) and the app runs without VNet integration.
Files are compared by SHA-256, so only changed files are transferred.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cad2anything.store.blob import DEFAULT_PREFIXES

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workspace_file (
    path text PRIMARY KEY,
    sha256 text NOT NULL,
    size bigint NOT NULL,
    content bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

UPSERT_SQL = """
INSERT INTO workspace_file (path, sha256, size, content, updated_at)
VALUES (%s, %s, %s, %s, now())
ON CONFLICT (path) DO UPDATE
SET sha256 = EXCLUDED.sha256, size = EXCLUDED.size, content = EXCLUDED.content, updated_at = now()
WHERE workspace_file.sha256 <> EXCLUDED.sha256
"""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_digest(path: Path) -> str | None:
    return _digest(path.read_bytes()) if path.is_file() else None


class PgMirror:
    def __init__(self, connect: Callable[[], Any] | None = None):
        if connect is None:
            from cad2anything.store.postgis import connect as pg_connect

            connect = pg_connect
        self._connect = connect
        self._conn: Any | None = None
        self._lock = threading.Lock()
        self._schema_ready = False

    @classmethod
    def from_env(cls) -> PgMirror | None:
        if not (os.environ.get("POSTGRES_HOST") or os.environ.get("PGHOST")):
            return None
        return cls()

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None, fetch: bool = False) -> list[Any]:
        """Run one autocommit statement, reconnecting once if the connection dropped."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                        self._conn.autocommit = True
                    if not self._schema_ready:
                        with self._conn.cursor() as cur:
                            cur.execute(TABLE_SQL)
                        self._schema_ready = True
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        return cur.fetchall() if fetch else []
                except Exception:
                    if attempt == 2 or self._conn is None or not self._conn.closed:
                        raise
                    self._conn = None
        return []

    def remote_index(self) -> dict[str, str]:
        return {
            path: sha for path, sha in self._execute("SELECT path, sha256 FROM workspace_file", fetch=True)
        }

    def fetch(self, name: str) -> bytes:
        rows = self._execute("SELECT content FROM workspace_file WHERE path = %s", (name,), fetch=True)
        if not rows:
            raise FileNotFoundError(name)
        return bytes(rows[0][0])

    def upsert(self, name: str, data: bytes) -> None:
        self._execute(UPSERT_SQL, (name, _digest(data), len(data), data))

    def push(self, path: Path, root: Path) -> None:
        name = Path(path).relative_to(root).as_posix()
        self.upsert(name, Path(path).read_bytes())

    def push_tree(self, root: Path, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> int:
        remote = self.remote_index()
        count = 0
        for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
            name = path.relative_to(root).as_posix()
            if not name.startswith(prefixes):
                continue
            data = path.read_bytes()
            if remote.get(name) == _digest(data):
                continue
            self.upsert(name, data)
            count += 1
        return count

    def pull(self, root: Path, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> int:
        count = 0
        for name, sha in sorted(self.remote_index().items()):
            if not name.startswith(prefixes):
                continue
            target = Path(root) / name
            if _local_digest(target) == sha:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.fetch(name))
            count += 1
        return count


def mirror_from_env() -> Any | None:
    """Pick the workspace mirror: WORKSPACE_MIRROR=postgres|blob|none, else Blob when configured."""
    kind = os.environ.get("WORKSPACE_MIRROR", "").strip().lower()
    if kind == "none":
        return None
    if kind == "postgres":
        mirror = PgMirror.from_env()
        if mirror is None:
            raise RuntimeError("WORKSPACE_MIRROR=postgres requires POSTGRES_HOST")
        return mirror
    if kind in ("blob", ""):
        from cad2anything.store.blob import BlobMirror

        mirror = BlobMirror.from_env() if os.environ.get("DATA_STORAGE_ACCOUNT") else None
        if mirror is None and kind == "blob":
            raise RuntimeError("WORKSPACE_MIRROR=blob requires DATA_STORAGE_ACCOUNT")
        return mirror
    raise RuntimeError(f"unknown WORKSPACE_MIRROR: {kind}")
