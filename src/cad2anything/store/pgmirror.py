"""Mirror workspace files to a PostgreSQL table using Microsoft Entra authentication.

Use this mirror when Blob Storage is reachable only through private endpoints (for example, when an
organization policy disables public network access) and the app runs without VNet integration.
Files are compared by SHA-256, so only changed files are transferred.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cad2anything.store.blob import DEFAULT_PREFIXES
from cad2anything.store.postgis import TOKEN_REFRESH_MARGIN_S
from cad2anything.workspace import atomic_write_bytes

LOCK_TIMEOUT_S = 40
logger = logging.getLogger("cad2anything.store.pgmirror")

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
    backend = "postgres"

    def __init__(self, connect: Callable[[], Any] | None = None):
        if connect is None:
            from cad2anything.store.postgis import connect as pg_connect

            connect = pg_connect
        self._connect = connect
        self._conn: Any | None = None
        self._lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._schema_ready = False
        self._available = False
        self.error_count = 0
        self.last_error: str | None = None
        self.last_error_at: str | None = None

    @classmethod
    def from_env(cls) -> PgMirror | None:
        if not (os.environ.get("POSTGRES_HOST") or os.environ.get("PGHOST")):
            return None
        return cls()

    def _usable_connection(self) -> bool:
        conn = self._conn
        if conn is None or conn.closed:
            return False
        expires_at = getattr(conn, "_cad2anything_token_expires_at", None)
        return expires_at is None or time.time() < expires_at - TOKEN_REFRESH_MARGIN_S

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        self._schema_ready = False
        if conn is not None:
            with suppress(Exception):
                conn.close()

    def _record_error(self, exc: Exception) -> None:
        with self._status_lock:
            self.error_count += 1
            self.last_error = type(exc).__name__
            self.last_error_at = datetime.now(UTC).isoformat()
            self._available = False
        logger.warning("workspace PostgreSQL mirror failed error_type=%s", type(exc).__name__)

    def health(self) -> dict[str, Any]:
        """Return cached status without making readiness depend on a live database query."""
        with self._status_lock:
            available = self._available
            errors = self.error_count
            last_error = self.last_error
            last_error_at = self.last_error_at
        return {
            "db": "ok" if available and self._usable_connection() else "down",
            "mirror_errors": errors,
            "last_mirror_error": last_error,
            "last_mirror_error_at": last_error_at,
        }

    def healthcheck(self) -> bool:
        """Refresh cached availability independently of the HTTP readiness probe."""
        try:
            self._execute("SELECT 1")
        except Exception:
            return False
        return True

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None, fetch: bool = False) -> list[Any]:
        """Run one autocommit statement; reconnect once on a broken or expiring connection."""
        import psycopg

        if not self._lock.acquire(timeout=LOCK_TIMEOUT_S):
            exc = TimeoutError("workspace mirror lock timed out")
            self._record_error(exc)
            raise exc
        try:
            for attempt in range(2):
                try:
                    if not self._usable_connection():
                        self._drop_connection()
                        self._conn = self._connect()
                        self._conn.autocommit = True
                    if not self._schema_ready:
                        with self._conn.cursor() as cur:
                            cur.execute(TABLE_SQL)
                        self._schema_ready = True
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        rows = cur.fetchall() if fetch else []
                    with self._status_lock:
                        self._available = True
                    return rows
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self._drop_connection()
                    self._record_error(exc)
                    if attempt == 0:
                        continue
                    raise
                except Exception as exc:
                    self._record_error(exc)
                    raise
        finally:
            self._lock.release()
        raise AssertionError("unreachable")

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
        for path in sorted(
            p
            for p in Path(root).rglob("*")
            if p.is_file() and not (p.name.startswith(".") and p.suffix == ".tmp")
        ):
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
            atomic_write_bytes(target, self.fetch(name))
            count += 1
        return count

    def delete(self, name: str) -> None:
        self._execute("DELETE FROM workspace_file WHERE path = %s", (name,))

    def prune(self, root: Path, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> int:
        """Delete mirrored files that no longer exist under ``root`` (for example after a demo reset)."""
        stale = [
            name
            for name in self.remote_index()
            if name.startswith(prefixes) and not (Path(root) / name).is_file()
        ]
        for name in stale:
            self.delete(name)
        return len(stale)


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
