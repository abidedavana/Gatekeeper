"""Typosquat similarity: Levenshtein distance and character-substitution patterns.

The Levenshtein implementation is the classic dynamic-programming algorithm
with two rolling rows: O(len(a) * len(b)) time, O(min(len(a), len(b))) space.
It never recurses, so there is no exponential blow-up.
"""

from __future__ import annotations

from dataclasses import dataclass

from .datasets import canonical_name, top_packages

MAX_TYPO_DISTANCE = 2

# Visually-confusable substitutions checked in addition to raw edit distance.
# Each pair is folded to a single canonical token; if two names collapse to the
# same folded form but differ literally, that is a substitution-pattern hit.
# Note: 'r' <-> 'rn' is an insertion/deletion of one character, so it is already
# covered by Levenshtein distance 1; the folding below covers 'rn' <-> 'm',
# '0' <-> 'o' and 'l' <-> '1'.
_FOLDS = (
    ("rn", "m"),
    ("0", "o"),
    ("1", "l"),
)


def levenshtein(a: str, b: str, max_distance: int | None = None) -> int:
    """Edit distance between *a* and *b* using two rolling arrays.

    If *max_distance* is given and the true distance exceeds it, returns
    ``max_distance + 1`` (early abandon) instead of the exact value.
    """
    if a == b:
        return 0
    # Keep b as the shorter string so the rows are as small as possible.
    if len(b) > len(a):
        a, b = b, a
    if max_distance is not None and len(a) - len(b) > max_distance:
        return max_distance + 1
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    current = [0] * (len(b) + 1)
    for i, ca in enumerate(a, start=1):
        current[0] = i
        row_min = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,  # deletion
                current[j - 1] + 1,  # insertion
                previous[j - 1] + cost,  # substitution
            )
            row_min = min(row_min, current[j])
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        previous, current = current, previous
    return previous[len(b)]


def fold_confusables(name: str) -> str:
    """Collapse visually-confusable sequences to one canonical form."""
    folded = name.lower()
    for src, dst in _FOLDS:
        folded = folded.replace(src, dst)
    return folded


@dataclass(frozen=True)
class SimilarMatch:
    """A popular package that a checked name closely resembles."""

    popular_name: str
    distance: int
    substitution_pattern: bool  # True when a confusable-fold made the names collide


def find_similar(name: str, registry: str) -> SimilarMatch | None:
    """Return the closest popular package within the typo threshold, or None.

    Exact members of the popular list are never reported as similar to
    themselves (they are the legitimate package).
    """
    canon = canonical_name(name, registry)
    populars = top_packages(registry)
    canon_populars = {canonical_name(p, registry): p for p in populars}
    if canon in canon_populars:
        return None

    folded = fold_confusables(canon)
    best: SimilarMatch | None = None
    for popular_canon, popular in canon_populars.items():
        if fold_confusables(popular_canon) == folded:
            # Substitution collision beats any distance-based match.
            return SimilarMatch(popular, levenshtein(canon, popular_canon), True)
        dist = levenshtein(canon, popular_canon, max_distance=MAX_TYPO_DISTANCE)
        if dist <= MAX_TYPO_DISTANCE and (best is None or dist < best.distance):
            best = SimilarMatch(popular, dist, False)
    return best
