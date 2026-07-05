"""Access to the bundled static data files."""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources

PINNED_PER_REGISTRY = 25  # first N of each list are permanently pinned in the cache (50 total)


def _load_json(filename: str) -> dict:
    ref = resources.files("gatekeeper.data").joinpath(filename)
    with ref.open("r", encoding="utf-8") as f:
        return json.load(f)


@cache
def top_packages(registry: str) -> tuple[str, ...]:
    """Popular package names for a registry ('pypi' or 'npm'), most popular first."""
    return tuple(_load_json("top_packages.json")[registry])


@cache
def pinned_packages(registry: str) -> frozenset[str]:
    """The permanently cache-pinned subset (top 25 per registry)."""
    return frozenset(top_packages(registry)[:PINNED_PER_REGISTRY])


@cache
def cooccurrence(registry: str) -> dict[str, list[str]]:
    """Static co-occurrence clusters used for prefetching."""
    data = _load_json("cooccurrence.json")[registry]
    return {k: v for k, v in data.items() if not k.startswith("_")}


def canonical_name(name: str, registry: str) -> str:
    """Canonical form for comparing names.

    PyPI treats names case-insensitively with '-', '_' and '.' equivalent
    (PEP 503). npm names are already lowercase-only, but we lowercase
    defensively and leave punctuation intact since '@scope/name', '-' and '.'
    are all significant on npm.
    """
    if registry == "pypi":
        return re.sub(r"[-_.]+", "-", name.lower())
    return name.lower()
