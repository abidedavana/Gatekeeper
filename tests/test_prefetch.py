"""Prefetch: fire-and-forget semantics — never blocks, never raises."""

from __future__ import annotations

import asyncio
import time

from gatekeeper.prefetch import drain, schedule_prefetch


class RecordingClient:
    """Stub RegistryClient that records prefetch requests."""

    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.fetched: list[tuple[str, str]] = []

    async def fetch_package(self, name: str, registry: str):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        self.fetched.append((name, registry))
        return None


class TestScheduling:
    async def test_torch_cluster_prefetched(self):
        client = RecordingClient()
        tasks = schedule_prefetch(client, ["torch"], "pypi")
        await drain(tasks)
        assert ("torchvision", "pypi") in client.fetched
        assert ("torchaudio", "pypi") in client.fetched

    async def test_already_checked_names_not_prefetched(self):
        client = RecordingClient()
        tasks = schedule_prefetch(client, ["torch", "torchvision"], "pypi")
        await drain(tasks)
        names = [n for n, _ in client.fetched]
        assert "torchvision" not in names  # was in the checked set already
        assert "torchaudio" in names

    async def test_unknown_package_schedules_nothing(self):
        client = RecordingClient()
        tasks = schedule_prefetch(client, ["obscure-package-zzz"], "pypi")
        assert tasks == []

    async def test_duplicates_deduped(self):
        client = RecordingClient()
        # pandas and scikit-learn clusters both contain numpy
        tasks = schedule_prefetch(client, ["pandas", "scikit-learn"], "pypi")
        await drain(tasks)
        names = [n for n, _ in client.fetched]
        assert names.count("numpy") == 1


class TestFireAndForget:
    async def test_prefetch_failure_never_raises(self):
        client = RecordingClient(fail=True)
        tasks = schedule_prefetch(client, ["torch"], "pypi")
        await drain(tasks)  # must not raise
        for task in tasks:
            assert task.done()
            assert task.exception() is None  # swallowed inside the task

    async def test_scheduling_returns_immediately(self):
        """schedule_prefetch must not await anything — the main result is
        rendered before prefetch completion is ever waited on."""
        client = RecordingClient(delay=5.0)
        start = time.perf_counter()
        tasks = schedule_prefetch(client, ["torch"], "pypi")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_drain_bounded_by_grace_period(self):
        client = RecordingClient(delay=30.0)
        tasks = schedule_prefetch(client, ["torch"], "pypi")
        start = time.perf_counter()
        await drain(tasks, grace=0.1)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0  # nowhere near the 30s the fetches would take
        assert all(t.done() for t in tasks)
