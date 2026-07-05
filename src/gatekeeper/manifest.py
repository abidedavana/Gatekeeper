"""Manifest parsing (requirements.txt / package.json) and manifest.safe output.

Correction policy for manifest.safe: a name is rewritten only when the
package was NOT FOUND on the registry and a close popular match exists —
i.e. an unambiguous typo. Packages that exist are never silently renamed
(a live typosquat should be flagged for a human, not swapped out); they are
reported instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .registry import STATUS_NOT_FOUND
from .scoring import CheckResult

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class ManifestError(ValueError):
    """Raised when a manifest cannot be read or its type cannot be determined."""


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    raw_line: str | None = None  # original requirements.txt line, if applicable
    spec: str | None = None  # version range from package.json, if applicable


def detect_type(path: Path) -> str | None:
    """'pip' | 'npm' from the filename, or None when ambiguous."""
    lower = path.name.lower()
    if lower == "package.json":
        return "npm"
    if lower.endswith(".txt") and "requirements" in lower:
        return "pip"
    return None


def parse_requirements(text: str) -> list[ManifestEntry]:
    """Extract package names from requirements.txt content.

    Skips comments, blank lines, pip options (-r/-e/--hash...), URLs and
    local paths. Extras, version specifiers and environment markers are
    stripped from the name but preserved via raw_line for rewriting.
    """
    entries: list[ManifestEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        if "://" in stripped or stripped.startswith((".", "/", "~")):
            continue
        match = _REQ_NAME_RE.match(stripped)
        if match:
            entries.append(ManifestEntry(name=match.group(1), raw_line=line))
    return entries


def parse_package_json(text: str) -> list[ManifestEntry]:
    """Extract names from dependencies + devDependencies of a package.json."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid package.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("invalid package.json: top level is not an object")
    entries: list[ManifestEntry] = []
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section) or {}
        if isinstance(deps, dict):
            for name, spec in deps.items():
                entries.append(ManifestEntry(name=name, spec=str(spec)))
    return entries


def load_manifest(path: Path, pkg_type: str | None = None) -> tuple[str, list[ManifestEntry]]:
    """Read a manifest file; returns (type, entries). *pkg_type* overrides
    filename auto-detection."""
    resolved = pkg_type or detect_type(path)
    if resolved is None:
        raise ManifestError(
            f"cannot determine manifest type of '{path.name}'; pass --type pip|npm"
        )
    # utf-8-sig: tolerate a UTF-8 BOM, which Windows editors and PowerShell
    # commonly prepend and which would otherwise glue itself to the first name.
    text = path.read_text(encoding="utf-8-sig")
    if resolved == "pip":
        return "pip", parse_requirements(text)
    return "npm", parse_package_json(text)


def _corrections(results: list[CheckResult]) -> dict[str, str]:
    """name -> corrected name, only for not-found packages with a suggestion."""
    return {
        r.info.name: r.suggestion
        for r in results
        if r.info.status == STATUS_NOT_FOUND and r.suggestion
    }


def write_safe_manifest(
    source: Path, pkg_type: str, results: list[CheckResult], output: Path | None = None
) -> tuple[Path, dict[str, str]]:
    """Write the cleaned manifest next to *source* (default: manifest.safe).

    Returns (path_written, corrections_applied).
    """
    output = output or source.parent / "manifest.safe"
    corrections = _corrections(results)
    text = source.read_text(encoding="utf-8-sig")

    if pkg_type == "pip":
        lines_out = []
        for line in text.splitlines():
            match = _REQ_NAME_RE.match(line.strip())
            if match and match.group(1) in corrections:
                fixed = corrections[match.group(1)]
                line = line.replace(match.group(1), fixed, 1)
            lines_out.append(line)
        output.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    else:
        data = json.loads(text)
        for section in ("dependencies", "devDependencies"):
            deps = data.get(section)
            if isinstance(deps, dict):
                data[section] = {corrections.get(k, k): v for k, v in deps.items()}
        output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return output, corrections
