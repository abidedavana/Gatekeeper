"""SQLite-backed metadata cache (~/.gatekeeper/cache.db).

Design notes
------------
* WAL journal mode so concurrent readers never block the single writer.
* Every statement runs through a retry-with-exponential-backoff wrapper for
  ``SQLITE_BUSY`` / "database is locked" errors raised when several
  gatekeeper processes hit the file at once. ``busy_timeout`` is also set so
  SQLite itself waits before surfacing BUSY.
* Normal entries expire after 24 hours (checked lazily on read).
* Entries for the top-50 pinned packages (see ``datasets.pinned_packages``)
  never expire and are never evicted.
* Eviction is LRU over NON-pinned rows only, keyed on
  (last_access, access_count), and only runs once the non-pinned row count
  exceeds ``max_unpinned`` (500). The whole eviction pass — count, select
  victims, delete — happens inside one ``BEGIN IMMEDIATE`` transaction.
  BEGIN IMMEDIATE takes SQLite's single write lock up front, so two
  processes can never interleave the same eviction pass: the second one
  blocks (or backs off and retries) until the first commits, then re-counts.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .datasets import canonical_name, pinned_packages

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".gatekeeper" / "cache.db"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours
DEFAULT_MAX_UNPINNED = 500

_BUSY_RETRIES = 6
_BUSY_BASE_DELAY = 0.05  # seconds; doubles each attempt, plus jitter

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS package_cache (
    registry     TEXT NOT NULL,
    name         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    fetched_at   REAL NOT NULL,
    last_access  REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    pinned       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (registry, name)
);
CREATE INDEX IF NOT EXISTS idx_lru
    ON package_cache (pinned, last_access, access_count);
"""


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


class Cache:
    """Package-metadata cache. Safe for use from multiple processes."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_unpinned: int = DEFAULT_MAX_UNPINNED,
    ) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self.ttl_seconds = ttl_seconds
        self.max_unpinned = max_unpinned
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=5.0)
        # The WAL switch and schema creation both need the write lock, and a
        # lock-upgrade conflict can surface as SQLITE_BUSY immediately
        # (bypassing the busy handler), so run setup through the retry path.
        self._retry(self._init_db)

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    # -- busy handling -----------------------------------------------------

    def _retry(self, fn: Callable[[], T]) -> T:
        """Run *fn*, retrying with exponential backoff on SQLITE_BUSY."""
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(_BUSY_RETRIES):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc):
                    raise
                last_exc = exc
                # Roll back any half-open transaction before retrying.
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                delay = min(_BUSY_BASE_DELAY * (2**attempt), 1.0) + random.uniform(0, 0.05)
                logger.debug("cache busy (attempt %d), retrying in %.2fs", attempt + 1, delay)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # -- public API ---------------------------------------------------------

    def get(self, registry: str, name: str) -> dict[str, Any] | None:
        """Return the cached payload, or None on miss/expiry. Updates LRU stats."""
        key = canonical_name(name, registry)

        def op() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT payload, fetched_at, pinned FROM package_cache"
                " WHERE registry = ? AND name = ?",
                (registry, key),
            ).fetchone()
            if row is None:
                return None
            payload, fetched_at, pinned = row
            now = time.time()
            if not pinned and now - fetched_at > self.ttl_seconds:
                self._conn.execute(
                    "DELETE FROM package_cache WHERE registry = ? AND name = ?",
                    (registry, key),
                )
                return None
            self._conn.execute(
                "UPDATE package_cache SET last_access = ?, access_count = access_count + 1"
                " WHERE registry = ? AND name = ?",
                (now, registry, key),
            )
            return json.loads(payload)

        return self._retry(op)

    def put(self, registry: str, name: str, payload: dict[str, Any]) -> None:
        """Store a payload; may trigger an LRU eviction pass (single-writer)."""
        key = canonical_name(name, registry)
        pinned = int(key in {canonical_name(p, registry) for p in pinned_packages(registry)})
        blob = json.dumps(payload)

        def op() -> None:
            now = time.time()
            # BEGIN IMMEDIATE = take the write lock now, making the whole
            # insert+evict pass atomic with respect to other processes.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO package_cache"
                    " (registry, name, payload, fetched_at, last_access, access_count, pinned)"
                    " VALUES (?, ?, ?, ?, ?, 0, ?)"
                    " ON CONFLICT(registry, name) DO UPDATE SET"
                    "   payload = excluded.payload, fetched_at = excluded.fetched_at,"
                    "   last_access = excluded.last_access, pinned = excluded.pinned",
                    (registry, key, blob, now, now, pinned),
                )
                (unpinned,) = self._conn.execute(
                    "SELECT COUNT(*) FROM package_cache WHERE pinned = 0"
                ).fetchone()
                excess = unpinned - self.max_unpinned
                if excess > 0:
                    self._conn.execute(
                        "DELETE FROM package_cache WHERE rowid IN ("
                        "  SELECT rowid FROM package_cache WHERE pinned = 0"
                        "  ORDER BY last_access ASC, access_count ASC LIMIT ?)",
                        (excess,),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

        self._retry(op)

    def status(self) -> dict[str, Any]:
        """Summary counters for `gatekeeper cache status`."""

        def op() -> dict[str, Any]:
            total, pinned = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(pinned), 0) FROM package_cache"
            ).fetchone()
            cutoff = time.time() - self.ttl_seconds
            (expired,) = self._conn.execute(
                "SELECT COUNT(*) FROM package_cache WHERE pinned = 0 AND fetched_at < ?",
                (cutoff,),
            ).fetchone()
            return {
                "path": str(self.path),
                "total_entries": total,
                "pinned_entries": pinned,
                "unpinned_entries": total - pinned,
                "expired_entries": expired,
                "max_unpinned": self.max_unpinned,
                "ttl_seconds": self.ttl_seconds,
                "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            }

        return self._retry(op)

    def clear(self) -> int:
        """Delete every entry (pinned included). Returns the number removed."""

        def op() -> int:
            cur = self._conn.execute("DELETE FROM package_cache")
            return cur.rowcount

        return self._retry(op)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
