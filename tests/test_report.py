"""JSON report schema stability and exit-code policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gatekeeper.registry import STATUS_ERROR, STATUS_NOT_FOUND, STATUS_OK, PackageInfo
from gatekeeper.report import SCHEMA_VERSION, build_json, exit_code
from gatekeeper.scoring import evaluate

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def result(name="pkg", status=STATUS_OK, **kwargs):
    defaults = dict(
        registry="pypi",
        first_release=NOW - timedelta(days=400),
        release_count=10,
    )
    defaults.update(kwargs)
    return evaluate(PackageInfo(name=name, status=status, **defaults), now=NOW)


class TestJsonSchema:
    def test_top_level_keys(self):
        doc = build_json([result()])
        assert doc["schema_version"] == SCHEMA_VERSION
        assert set(doc) == {"schema_version", "tool", "generated_at", "results", "summary"}

    def test_result_keys_stable(self):
        (entry,) = build_json([result()])["results"]
        assert set(entry) == {
            "name", "registry", "status", "error", "score", "level",
            "signals", "suggestion", "cached", "metadata",
        }
        assert set(entry["metadata"]) == {
            "first_release", "release_count", "repo_url",
            "github_owner", "github_owner_created", "github_signal",
        }

    def test_github_signal_marked_unavailable_without_repo(self):
        (entry,) = build_json([result()])["results"]
        assert entry["metadata"]["github_signal"] == "unavailable"

    def test_summary_counts(self):
        doc = build_json([
            result("a"),
            result("requessts", status=STATUS_NOT_FOUND, first_release=None, release_count=None),
            result("b", status=STATUS_ERROR, error="timeout",
                   first_release=None, release_count=None),
        ])
        summary = doc["summary"]
        assert summary["total"] == 3
        assert summary["low"] == 1
        assert summary["not_found"] == 1
        assert summary["errors"] == 1


class TestExitCode:
    def test_clean_is_zero(self):
        assert exit_code([result()]) == 0

    def test_not_found_fails(self):
        r = result("ghost", status=STATUS_NOT_FOUND, first_release=None, release_count=None)
        assert exit_code([r]) == 1

    def test_high_risk_fails(self):
        # distance-1 typosquat (40) + new package (30) = 70 -> HIGH
        r = result("requessts", first_release=NOW - timedelta(days=1))
        assert r.level == "HIGH"
        assert exit_code([r]) == 1

    def test_error_passes_unless_strict(self):
        r = result("flaky", status=STATUS_ERROR, error="timeout",
                   first_release=None, release_count=None)
        assert exit_code([r]) == 0
        assert exit_code([r], strict=True) == 1
