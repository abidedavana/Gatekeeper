"""Fire-and-forget cache prefetch from a static co-occurrence map.

Checking "torch" also warms the cache for "torchvision" and "torchaudio", etc.
Guarantees:

* The main check/audit result is computed and RENDERED before prefetch
  completion is ever awaited — prefetch can only run concurrently with, never
  ahead of or instead of, the primary work.
* ``drain`` waits at most ``grace`` seconds after output is produced, then
  cancels stragglers, so process exit is bounded too.
* Every prefetch task swallows and logs its own exceptions; a prefetch
  failure can never propagate into the main result.
"""

from __future__ import annotations

import asyncio
import logging

from .datasets import canonical_name, cooccurrence
from .registry import RegistryClient

logger = logging.getLogger(__name__)

DEFAULT_GRACE_SECONDS = 2.0


async def _prefetch_one(client: RegistryClient, name: str, registry: str) -> None:
    try:
        await client.fetch_package(name, registry)
        logger.debug("prefetched %s/%s", registry, name)
    except Exception as exc:  # never raise out of a prefetch task
        logger.debug("prefetch failed for %s/%s: %s", registry, name, exc)


def schedule_prefetch(
    client: RegistryClient, checked_names: list[str], registry: str
) -> list[asyncio.Task]:
    """Create background tasks for co-occurring packages of *checked_names*.

    Returns immediately; the returned tasks run on the event loop alongside
    whatever the caller does next.
    """
    comap = cooccurrence(registry)
    checked = {canonical_name(n, registry) for n in checked_names}
    targets: list[str] = []
    seen: set[str] = set()
    for name in checked_names:
        for related in comap.get(canonical_name(name, registry), []):
            canon = canonical_name(related, registry)
            if canon not in checked and canon not in seen:
                seen.add(canon)
                targets.append(related)
    return [
        asyncio.create_task(_prefetch_one(client, t, registry), name=f"prefetch:{t}")
        for t in targets
    ]


async def drain(tasks: list[asyncio.Task], grace: float = DEFAULT_GRACE_SECONDS) -> None:
    """Give outstanding prefetch tasks up to *grace* seconds, then cancel them."""
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=grace)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.debug("cancelled %d unfinished prefetch task(s)", len(pending))
