"""End-to-end CLI tests with mocked HTTP (no live network)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from gatekeeper.cli import cli
from gatekeeper.registry import FetchOutcome, RegistryClient
from tests.conftest import npm_payload, pypi_payload

ENV = {"GATEKEEPER_NO_PREFETCH": "1", "GATEKEEPER_NO_GITHUB": "1"}


@pytest.fixture
def http_script(monkeypatch):
    """URL-fragment -> FetchOutcome map patched over RegistryClient._get_json."""
    script: dict[str, FetchOutcome] = {}

    async def fake_get_json(self, url, headers=None):
        for fragment, outcome in script.items():
            if fragment in url:
                return outcome
        return FetchOutcome(404, data={"message": "Not Found"})

    monkeypatch.setattr(RegistryClient, "_get_json", fake_get_json)
    return script


@pytest.fixture
def runner():
    return CliRunner()


def base_args(tmp_path) -> list[str]:
    return ["--cache-path", str(tmp_path / "cache.db")]


class TestCheck:
    def test_clean_package_json_output(self, runner, http_script, tmp_path):
        http_script["pypi.org/pypi/requests/"] = FetchOutcome(200, data=pypi_payload("requests"))
        result = runner.invoke(
            cli, [*base_args(tmp_path), "check", "requests", "--json"], env=ENV
        )
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        entry = doc["results"][0]
        assert entry["name"] == "requests"
        assert entry["status"] == "ok"
        assert entry["level"] == "LOW"

    def test_typosquat_not_found_suggests_and_fails(self, runner, http_script, tmp_path):
        result = runner.invoke(
            cli, [*base_args(tmp_path), "check", "requessts", "--json"], env=ENV
        )
        assert result.exit_code == 1
        entry = json.loads(result.output)["results"][0]
        assert entry["status"] == "not_found"
        assert entry["suggestion"] == "requests"

    def test_npm_type(self, runner, http_script, tmp_path):
        http_script["registry.npmjs.org/lodash"] = FetchOutcome(200, data=npm_payload("lodash"))
        result = runner.invoke(
            cli, [*base_args(tmp_path), "check", "lodash", "--type", "npm", "--json"], env=ENV
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["results"][0]["registry"] == "npm"

    def test_table_output(self, runner, http_script, tmp_path):
        http_script["pypi.org"] = FetchOutcome(200, data=pypi_payload("requests"))
        result = runner.invoke(cli, [*base_args(tmp_path), "check", "requests"], env=ENV)
        assert result.exit_code == 0
        assert "Gatekeeper report" in result.output

    def test_registry_error_reported_not_crash(self, runner, http_script, tmp_path):
        http_script["pypi.org"] = FetchOutcome(None, error="timeout after 10s")
        result = runner.invoke(
            cli, [*base_args(tmp_path), "check", "somepkg", "--json"], env=ENV
        )
        assert result.exit_code == 0  # errors are non-fatal by default
        entry = json.loads(result.output)["results"][0]
        assert entry["status"] == "error"
        assert "timeout" in entry["error"]


class TestAudit:
    def test_audit_writes_manifest_safe(self, runner, http_script, tmp_path):
        manifest = tmp_path / "requirements.txt"
        manifest.write_text("requests==2.31.0\nrequessts==1.0\n", encoding="utf-8")
        http_script["pypi.org/pypi/requests/"] = FetchOutcome(200, data=pypi_payload("requests"))
        # requessts falls through to the default 404

        result = runner.invoke(
            cli, [*base_args(tmp_path), "audit", str(manifest), "--json"], env=ENV
        )
        assert result.exit_code == 1  # not-found package present
        doc = json.loads(result.output)
        assert doc["summary"]["not_found"] == 1

        safe = (tmp_path / "manifest.safe").read_text(encoding="utf-8")
        assert "requests==2.31.0" in safe
        assert "requests==1.0" in safe  # corrected typo keeps its pin
        assert "requessts" not in safe

    def test_audit_package_json(self, runner, http_script, tmp_path):
        manifest = tmp_path / "package.json"
        manifest.write_text(
            json.dumps({"dependencies": {"lodash": "^4.0.0"}}), encoding="utf-8"
        )
        http_script["registry.npmjs.org/lodash"] = FetchOutcome(200, data=npm_payload("lodash"))
        result = runner.invoke(
            cli, [*base_args(tmp_path), "audit", str(manifest), "--json"], env=ENV
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["results"][0]["registry"] == "npm"

    def test_unknown_manifest_type_is_clean_error(self, runner, tmp_path):
        manifest = tmp_path / "deps.lock"
        manifest.write_text("requests\n", encoding="utf-8")
        result = runner.invoke(cli, [*base_args(tmp_path), "audit", str(manifest)], env=ENV)
        assert result.exit_code != 0
        assert "--type" in result.output

    def test_strict_fails_on_lookup_errors(self, runner, http_script, tmp_path):
        manifest = tmp_path / "requirements.txt"
        manifest.write_text("somepkg\n", encoding="utf-8")
        http_script["pypi.org"] = FetchOutcome(None, error="timeout after 10s")
        relaxed = runner.invoke(
            cli, [*base_args(tmp_path), "audit", str(manifest)], env=ENV
        )
        assert relaxed.exit_code == 0
        strict = runner.invoke(
            cli, [*base_args(tmp_path), "audit", str(manifest), "--strict"], env=ENV
        )
        assert strict.exit_code == 1


class TestCacheCommands:
    def test_status_and_clear(self, runner, http_script, tmp_path):
        http_script["pypi.org"] = FetchOutcome(200, data=pypi_payload("requests"))
        runner.invoke(cli, [*base_args(tmp_path), "check", "requests", "--json"], env=ENV)

        status = runner.invoke(cli, [*base_args(tmp_path), "cache", "status"], env=ENV)
        assert status.exit_code == 0
        assert "total_entries" in status.output

        cleared = runner.invoke(cli, [*base_args(tmp_path), "cache", "clear"], env=ENV)
        assert cleared.exit_code == 0
        assert "Removed" in cleared.output
