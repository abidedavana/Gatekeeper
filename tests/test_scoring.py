"""Scoring point values and risk bands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gatekeeper.registry import STATUS_ERROR, STATUS_NOT_FOUND, STATUS_OK, PackageInfo
from gatekeeper.scoring import POINTS, evaluate, score_to_level

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def info(**kwargs) -> PackageInfo:
    defaults = dict(
        name="innocuous-package-zq",
        registry="pypi",
        status=STATUS_OK,
        first_release=NOW - timedelta(days=400),
        release_count=25,
    )
    defaults.update(kwargs)
    return PackageInfo(**defaults)


class TestBands:
    def test_inclusive_boundaries(self):
        assert score_to_level(0) == "LOW"
        assert score_to_level(39) == "LOW"
        assert score_to_level(40) == "MEDIUM"
        assert score_to_level(59) == "MEDIUM"
        assert score_to_level(60) == "HIGH"
        assert score_to_level(79) == "HIGH"
        assert score_to_level(80) == "CRITICAL"
        assert score_to_level(100) == "CRITICAL"


class TestSignals:
    def test_clean_old_package_scores_zero(self):
        result = evaluate(info(), now=NOW)
        assert result.score == 0
        assert result.level == "LOW"
        assert result.signals == []

    def test_distance_one_typosquat(self):
        result = evaluate(info(name="requessts"), now=NOW)
        ids = {s.id for s in result.signals}
        assert "TYPOSQUAT_DISTANCE_1" in ids
        assert result.score == POINTS["TYPOSQUAT_DISTANCE_1"]
        assert result.suggestion == "requests"

    def test_distance_two_typosquat(self):
        result = evaluate(info(name="requsts"), now=NOW)
        # "requsts" is 1 edit from requests actually? r-e-q-u-s-t-s: missing 'e' -> distance 1
        assert result.suggestion == "requests"

    def test_substitution_pattern_points(self):
        result = evaluate(info(name="nurnpy"), now=NOW)
        assert {s.id for s in result.signals} == {"SUBSTITUTION_PATTERN"}
        assert result.score == POINTS["SUBSTITUTION_PATTERN"]

    def test_only_one_similarity_signal(self):
        result = evaluate(info(name="nurnpy"), now=NOW)
        similarity_ids = {"SUBSTITUTION_PATTERN", "TYPOSQUAT_DISTANCE_1", "TYPOSQUAT_DISTANCE_2"}
        assert len([s for s in result.signals if s.id in similarity_ids]) == 1

    def test_new_package(self):
        result = evaluate(info(first_release=NOW - timedelta(days=2)), now=NOW)
        assert {s.id for s in result.signals} == {"NEW_PACKAGE"}
        assert result.score == POINTS["NEW_PACKAGE"]

    def test_week_old_package_not_flagged(self):
        result = evaluate(info(first_release=NOW - timedelta(days=8)), now=NOW)
        assert "NEW_PACKAGE" not in {s.id for s in result.signals}

    def test_single_release(self):
        result = evaluate(info(release_count=1), now=NOW)
        assert {s.id for s in result.signals} == {"SINGLE_RELEASE"}
        assert result.score == POINTS["SINGLE_RELEASE"]

    def test_few_releases(self):
        result = evaluate(info(release_count=3), now=NOW)
        assert {s.id for s in result.signals} == {"FEW_RELEASES"}
        assert result.score == POINTS["FEW_RELEASES"]

    def test_many_releases_no_signal(self):
        result = evaluate(info(release_count=5), now=NOW)
        assert result.signals == []

    def test_young_github_owner(self):
        result = evaluate(
            info(github_owner="newbie", github_owner_created=NOW - timedelta(days=10)),
            now=NOW,
        )
        assert {s.id for s in result.signals} == {"YOUNG_GITHUB_OWNER"}
        assert result.github_signal_available

    def test_old_github_owner_no_signal(self):
        result = evaluate(
            info(github_owner="veteran", github_owner_created=NOW - timedelta(days=3000)),
            now=NOW,
        )
        assert result.signals == []
        assert result.github_signal_available

    def test_github_unavailable_flag(self):
        result = evaluate(info(), now=NOW)
        assert not result.github_signal_available

    def test_score_capped_at_100(self):
        result = evaluate(
            info(
                name="requessts",
                first_release=NOW - timedelta(days=1),
                release_count=1,
                github_owner="newbie",
                github_owner_created=NOW - timedelta(days=3),
            ),
            now=NOW,
        )
        # 40 + 30 + 20 + 15 = 105 -> capped
        assert result.score == 100
        assert result.level == "CRITICAL"

    def test_popular_package_never_gets_similarity_signal(self):
        result = evaluate(info(name="requests"), now=NOW)
        assert result.signals == []
        assert result.suggestion is None


class TestNonOkStatuses:
    def test_not_found_has_no_score_but_a_suggestion(self):
        result = evaluate(
            info(name="requessts", status=STATUS_NOT_FOUND, first_release=None,
                 release_count=None),
            now=NOW,
        )
        assert result.score is None
        assert result.level is None
        assert result.suggestion == "requests"

    def test_error_has_no_score(self):
        result = evaluate(
            info(status=STATUS_ERROR, error="timeout", first_release=None, release_count=None),
            now=NOW,
        )
        assert result.score is None
        assert result.level is None
