"""SQLite cache: TTL, pinning, LRU eviction, concurrency."""

from __future__ import annotations

import sqlite3
import threading

from gatekeeper.cache import Cache


def payload(n: int = 0) -> dict:
    return {"name": f"pkg{n}", "registry": "pypi", "status": "ok",
            "first_release": None, "release_count": n, "repo_url": None,
            "github_owner": None, "github_owner_created": None, "github_checked": False}


class TestBasics:
    def test_roundtrip(self, tmp_cache):
        tmp_cache.put("pypi", "leftpadish", payload(1))
        assert tmp_cache.get("pypi", "leftpadish")["release_count"] == 1

    def test_miss(self, tmp_cache):
        assert tmp_cache.get("pypi", "never-stored") is None

    def test_registries_are_separate_namespaces(self, tmp_cache):
        tmp_cache.put("pypi", "shared-name", payload(1))
        assert tmp_cache.get("npm", "shared-name") is None

    def test_name_canonicalization_pypi(self, tmp_cache):
        tmp_cache.put("pypi", "Python_Dateutil", payload(1))
        assert tmp_cache.get("pypi", "python-dateutil") is not None

    def test_clear(self, tmp_cache):
        tmp_cache.put("pypi", "a", payload())
        tmp_cache.put("pypi", "requests", payload())  # pinned
        removed = tmp_cache.clear()
        assert removed == 2
        assert tmp_cache.get("pypi", "a") is None
        assert tmp_cache.get("pypi", "requests") is None

    def test_status_counts(self, tmp_cache):
        tmp_cache.put("pypi", "requests", payload())  # pinned (top-25 pypi)
        tmp_cache.put("pypi", "obscure-thing", payload())
        status = tmp_cache.status()
        assert status["total_entries"] == 2
        assert status["pinned_entries"] == 1
        assert status["unpinned_entries"] == 1


class TestExpiry:
    def test_expired_entry_is_a_miss(self, tmp_path):
        with Cache(path=tmp_path / "c.db", ttl_seconds=-1.0) as cache:
            cache.put("pypi", "obscure-thing", payload())
            assert cache.get("pypi", "obscure-thing") is None

    def test_pinned_entry_never_expires(self, tmp_path):
        with Cache(path=tmp_path / "c.db", ttl_seconds=-1.0) as cache:
            cache.put("pypi", "requests", payload())  # top-25 -> pinned
            assert cache.get("pypi", "requests") is not None


class TestEviction:
    def test_lru_eviction_over_limit(self, tmp_path):
        with Cache(path=tmp_path / "c.db", max_unpinned=5) as cache:
            for i in range(5):
                cache.put("pypi", f"filler-{i}", payload(i))
            # Touch filler-0 so it is the most recently used.
            assert cache.get("pypi", "filler-0") is not None
            # Two more puts must evict the two least-recently-used entries.
            cache.put("pypi", "filler-5", payload(5))
            cache.put("pypi", "filler-6", payload(6))
            assert cache.status()["unpinned_entries"] == 5
            assert cache.get("pypi", "filler-0") is not None  # recently touched
            assert cache.get("pypi", "filler-1") is None  # LRU, evicted
            assert cache.get("pypi", "filler-2") is None  # LRU, evicted
            assert cache.get("pypi", "filler-6") is not None

    def test_pinned_entries_survive_eviction(self, tmp_path):
        with Cache(path=tmp_path / "c.db", max_unpinned=3) as cache:
            cache.put("pypi", "requests", payload())  # pinned
            for i in range(10):
                cache.put("pypi", f"filler-{i}", payload(i))
            assert cache.get("pypi", "requests") is not None
            assert cache.status()["unpinned_entries"] <= 3


class TestConcurrency:
    def test_two_connections_interleaved_writes(self, tmp_path):
        """Two Cache instances on the same file (as two processes would be)
        write concurrently without raising SQLITE_BUSY to the caller."""
        path = tmp_path / "c.db"
        errors: list[Exception] = []

        def writer(offset: int) -> None:
            try:
                with Cache(path=path) as cache:
                    for i in range(30):
                        cache.put("pypi", f"pkg-{offset}-{i}", payload(i))
                        cache.get("pypi", f"pkg-{offset}-{i}")
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        with Cache(path=path) as cache:
            assert cache.status()["total_entries"] == 120

    def test_wal_mode_enabled(self, tmp_path):
        with Cache(path=tmp_path / "c.db"):
            conn = sqlite3.connect(str(tmp_path / "c.db"))
            (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            conn.close()
            assert mode.lower() == "wal"

    def test_eviction_is_single_transaction(self, tmp_path):
        """Concurrent puts around the eviction threshold never push the
        non-pinned count above the limit (the count+delete happens inside one
        BEGIN IMMEDIATE transaction)."""
        path = tmp_path / "c.db"
        errors: list[Exception] = []

        def writer(offset: int) -> None:
            try:
                with Cache(path=path, max_unpinned=20) as cache:
                    for i in range(40):
                        cache.put("pypi", f"pkg-{offset}-{i}", payload(i))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        with Cache(path=path, max_unpinned=20) as cache:
            assert cache.status()["unpinned_entries"] <= 20
