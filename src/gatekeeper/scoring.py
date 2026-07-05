"""Risk scoring: explicit, auditable point values per signal.

Signals and points (summed, capped at 100):

===========================  ======  ==================================================
Signal id                    Points  Trigger
===========================  ======  ==================================================
SUBSTITUTION_PATTERN             40  Name collides with a popular package after folding
                                     confusable sequences (rn<->m, 0<->o, l<->1;
                                     r<->rn is an edit-distance-1 case, see below)
TYPOSQUAT_DISTANCE_1             40  Levenshtein distance 1 from a popular package
TYPOSQUAT_DISTANCE_2             25  Levenshtein distance 2 from a popular package
NEW_PACKAGE                      30  First release less than 7 days ago
SINGLE_RELEASE                   20  Exactly one release
FEW_RELEASES                     10  2-4 releases
YOUNG_GITHUB_OWNER               15  Linked GitHub repo owner account < 90 days old
===========================  ======  ==================================================

At most ONE similarity signal is applied per package (substitution collision
takes precedence, then the smallest distance). Packages whose name is exactly
in the popular list never receive a similarity signal.

Risk bands (inclusive):
    LOW      0-39
    MEDIUM  40-59
    HIGH    60-79
    CRITICAL 80-100
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .registry import STATUS_OK, PackageInfo, utcnow
from .similarity import SimilarMatch, find_similar

POINTS = {
    "SUBSTITUTION_PATTERN": 40,
    "TYPOSQUAT_DISTANCE_1": 40,
    "TYPOSQUAT_DISTANCE_2": 25,
    "NEW_PACKAGE": 30,
    "SINGLE_RELEASE": 20,
    "FEW_RELEASES": 10,
    "YOUNG_GITHUB_OWNER": 15,
}

NEW_PACKAGE_WINDOW = timedelta(days=7)
YOUNG_OWNER_WINDOW = timedelta(days=90)
FEW_RELEASES_MAX = 4

LEVEL_LOW = "LOW"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_HIGH = "HIGH"
LEVEL_CRITICAL = "CRITICAL"


def score_to_level(score: int) -> str:
    """Map a 0-100 score to its band. Bands are inclusive on both ends."""
    if score <= 39:
        return LEVEL_LOW
    if score <= 59:
        return LEVEL_MEDIUM
    if score <= 79:
        return LEVEL_HIGH
    return LEVEL_CRITICAL


@dataclass(frozen=True)
class Signal:
    id: str
    points: int
    detail: str


@dataclass
class CheckResult:
    """Everything the reporters need about one checked package."""

    info: PackageInfo
    score: int | None  # None when status != ok (nothing to score)
    level: str | None
    signals: list[Signal] = field(default_factory=list)
    suggestion: str | None = None  # closest popular package, if any
    github_signal_available: bool = False

    @property
    def name(self) -> str:
        return self.info.name


def _similarity_signal(match: SimilarMatch) -> Signal | None:
    if match.substitution_pattern:
        return Signal(
            "SUBSTITUTION_PATTERN",
            POINTS["SUBSTITUTION_PATTERN"],
            f"confusable-character variant of popular package '{match.popular_name}'",
        )
    if match.distance == 1:
        return Signal(
            "TYPOSQUAT_DISTANCE_1",
            POINTS["TYPOSQUAT_DISTANCE_1"],
            f"1 edit away from popular package '{match.popular_name}'",
        )
    if match.distance == 2:
        return Signal(
            "TYPOSQUAT_DISTANCE_2",
            POINTS["TYPOSQUAT_DISTANCE_2"],
            f"2 edits away from popular package '{match.popular_name}'",
        )
    return None


def evaluate(info: PackageInfo, *, now: datetime | None = None) -> CheckResult:
    """Score a fetched package. For not_found/error statuses no score is
    produced, but a typo suggestion is still computed so the caller can offer
    a correction."""
    now = now or utcnow()
    match = find_similar(info.name, info.registry)
    suggestion = match.popular_name if match else None

    if info.status != STATUS_OK:
        return CheckResult(info=info, score=None, level=None, suggestion=suggestion)

    signals: list[Signal] = []
    if match is not None:
        sim = _similarity_signal(match)
        if sim is not None:
            signals.append(sim)

    if info.first_release is not None:
        age = now - info.first_release
        if age < NEW_PACKAGE_WINDOW:
            signals.append(
                Signal(
                    "NEW_PACKAGE",
                    POINTS["NEW_PACKAGE"],
                    f"first release only {max(age.days, 0)} day(s) ago",
                )
            )

    if info.release_count is not None and info.release_count > 0:
        if info.release_count == 1:
            signals.append(Signal("SINGLE_RELEASE", POINTS["SINGLE_RELEASE"], "only one release"))
        elif info.release_count <= FEW_RELEASES_MAX:
            signals.append(
                Signal(
                    "FEW_RELEASES",
                    POINTS["FEW_RELEASES"],
                    f"only {info.release_count} releases",
                )
            )

    github_available = info.github_owner_created is not None
    if github_available and now - info.github_owner_created < YOUNG_OWNER_WINDOW:
        owner_age = (now - info.github_owner_created).days
        signals.append(
            Signal(
                "YOUNG_GITHUB_OWNER",
                POINTS["YOUNG_GITHUB_OWNER"],
                f"GitHub owner '{info.github_owner}' account is {owner_age} day(s) old",
            )
        )

    score = min(sum(s.points for s in signals), 100)
    return CheckResult(
        info=info,
        score=score,
        level=score_to_level(score),
        signals=signals,
        suggestion=suggestion,
        github_signal_available=github_available,
    )
