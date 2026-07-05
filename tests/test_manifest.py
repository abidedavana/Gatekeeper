"""Manifest parsing and manifest.safe generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gatekeeper.manifest import (
    ManifestError,
    detect_type,
    load_manifest,
    parse_package_json,
    parse_requirements,
    write_safe_manifest,
)
from gatekeeper.registry import STATUS_NOT_FOUND, STATUS_OK, PackageInfo
from gatekeeper.scoring import CheckResult

REQUIREMENTS = """\
# main deps
requests==2.31.0
flask>=2.0  # comment after spec
numpy
pandas[performance]>=2.0; python_version >= "3.10"

-r other-requirements.txt
--hash=sha256:deadbeef
-e ./local-package
https://example.com/direct.whl
./relative/path
"""


def result(name: str, status: str = STATUS_OK, suggestion: str | None = None) -> CheckResult:
    info = PackageInfo(name=name, registry="pypi", status=status)
    return CheckResult(info=info, score=0 if status == STATUS_OK else None,
                       level="LOW" if status == STATUS_OK else None, suggestion=suggestion)


class TestDetectType:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("requirements.txt", "pip"),
            ("requirements-dev.txt", "pip"),
            ("dev-requirements.txt", "pip"),
            ("package.json", "npm"),
            ("Pipfile", None),
            ("deps.txt", None),
        ],
    )
    def test_detection(self, filename, expected):
        assert detect_type(Path(filename)) == expected


class TestParseRequirements:
    def test_names_extracted(self):
        names = [e.name for e in parse_requirements(REQUIREMENTS)]
        assert names == ["requests", "flask", "numpy", "pandas"]

    def test_options_urls_and_paths_skipped(self):
        names = [e.name for e in parse_requirements(REQUIREMENTS)]
        assert "other-requirements.txt" not in names
        assert not any("example.com" in n for n in names)

    def test_empty_file(self):
        assert parse_requirements("") == []


class TestParsePackageJson:
    def test_deps_and_dev_deps(self):
        text = json.dumps({
            "name": "app",
            "dependencies": {"react": "^18.0.0", "lodash": "^4.17.21"},
            "devDependencies": {"jest": "^29.0.0"},
        })
        names = [e.name for e in parse_package_json(text)]
        assert names == ["react", "lodash", "jest"]

    def test_invalid_json_raises_manifest_error(self):
        with pytest.raises(ManifestError):
            parse_package_json("{not json")

    def test_missing_sections_ok(self):
        assert parse_package_json('{"name": "app"}') == []


class TestLoadManifest:
    def test_utf8_bom_tolerated(self, tmp_path):
        """Windows editors/PowerShell prepend a BOM; the first package name
        must not be lost to it."""
        path = tmp_path / "requirements.txt"
        path.write_bytes(b"\xef\xbb\xbftorch\nnumpy\n")
        _, entries = load_manifest(path)
        assert [e.name for e in entries] == ["torch", "numpy"]

    def test_type_override(self, tmp_path):
        path = tmp_path / "deps.txt"
        path.write_text("requests\n", encoding="utf-8")
        kind, entries = load_manifest(path, "pip")
        assert kind == "pip"
        assert entries[0].name == "requests"

    def test_undetectable_without_override(self, tmp_path):
        path = tmp_path / "deps.txt"
        path.write_text("requests\n", encoding="utf-8")
        with pytest.raises(ManifestError):
            load_manifest(path)


class TestWriteSafeManifest:
    def test_pip_correction_applied_for_not_found_typo(self, tmp_path):
        src = tmp_path / "requirements.txt"
        src.write_text("requessts==2.31.0\nnumpy\n", encoding="utf-8")
        results = [
            result("requessts", STATUS_NOT_FOUND, suggestion="requests"),
            result("numpy"),
        ]
        out, corrections = write_safe_manifest(src, "pip", results)
        assert out == tmp_path / "manifest.safe"
        assert corrections == {"requessts": "requests"}
        content = out.read_text(encoding="utf-8")
        assert "requests==2.31.0" in content
        assert "requessts" not in content
        assert "numpy" in content

    def test_existing_package_is_never_renamed(self, tmp_path):
        """A live typosquat (found on the registry) is flagged, not swapped."""
        src = tmp_path / "requirements.txt"
        src.write_text("requessts==1.0\n", encoding="utf-8")
        results = [result("requessts", STATUS_OK, suggestion="requests")]
        out, corrections = write_safe_manifest(src, "pip", results)
        assert corrections == {}
        assert "requessts==1.0" in out.read_text(encoding="utf-8")

    def test_npm_correction(self, tmp_path):
        src = tmp_path / "package.json"
        src.write_text(
            json.dumps({"dependencies": {"lodahs": "^4.0.0", "react": "^18.0.0"}}),
            encoding="utf-8",
        )
        results = [
            result("lodahs", STATUS_NOT_FOUND, suggestion="lodash"),
            result("react"),
        ]
        out, corrections = write_safe_manifest(src, "npm", results)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert corrections == {"lodahs": "lodash"}
        assert data["dependencies"] == {"lodash": "^4.0.0", "react": "^18.0.0"}

    def test_custom_output_path(self, tmp_path):
        src = tmp_path / "requirements.txt"
        src.write_text("numpy\n", encoding="utf-8")
        target = tmp_path / "cleaned.txt"
        out, _ = write_safe_manifest(src, "pip", [result("numpy")], output=target)
        assert out == target
        assert target.exists()
