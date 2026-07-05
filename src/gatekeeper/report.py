"""Terminal (rich) and JSON reporters.

The JSON schema is stable and documented in README.md; bump
``SCHEMA_VERSION`` on any breaking change.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from rich.console import Console
from rich.table import Table

from . import __version__
from .registry import STATUS_ERROR, STATUS_NOT_FOUND, STATUS_OK, utcnow
from .scoring import (
    LEVEL_CRITICAL,
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    CheckResult,
)

SCHEMA_VERSION = 1

_LEVEL_STYLE = {
    LEVEL_LOW: ("✅", "green"),
    LEVEL_MEDIUM: ("🟡", "yellow"),
    LEVEL_HIGH: ("🟠", "dark_orange"),
    LEVEL_CRITICAL: ("🚨", "red"),
}
_STATUS_EMOJI = {STATUS_NOT_FOUND: "❓", STATUS_ERROR: "⚠️"}


def _row(result: CheckResult) -> tuple[str, ...]:
    info = result.info
    if info.status == STATUS_OK:
        emoji, style = _LEVEL_STYLE[result.level]
        verdict = f"{emoji} [{style}]{result.level} ({result.score})[/{style}]"
    elif info.status == STATUS_NOT_FOUND:
        verdict = f"{_STATUS_EMOJI[info.status]} [magenta]NOT FOUND[/magenta]"
    else:
        verdict = f"{_STATUS_EMOJI[info.status]} [red]ERROR: {info.error}[/red]"

    signals = "\n".join(f"+{s.points} {s.detail}" for s in result.signals) or "—"
    if info.status == STATUS_OK and not result.github_signal_available:
        signals += "\n[dim](GitHub owner-age signal unavailable)[/dim]"
    suggestion = f"did you mean [bold]{result.suggestion}[/bold]?" if result.suggestion else "—"
    cached = "yes" if info.cached else "no"
    return (info.name, info.registry, verdict, signals, suggestion, cached)


def render_table(results: list[CheckResult], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="Gatekeeper report", show_lines=True)
    table.add_column("Package", style="bold")
    table.add_column("Registry")
    table.add_column("Risk")
    table.add_column("Signals")
    table.add_column("Suggestion")
    table.add_column("Cached")
    for result in results:
        table.add_row(*_row(result))
    console.print(table)


def build_json(results: list[CheckResult]) -> dict[str, Any]:
    """Stable machine-readable report for --json / CI."""

    def one(result: CheckResult) -> dict[str, Any]:
        info = result.info
        return {
            "name": info.name,
            "registry": info.registry,
            "status": info.status,
            "error": info.error,
            "score": result.score,
            "level": result.level,
            "signals": [
                {"id": s.id, "points": s.points, "detail": s.detail} for s in result.signals
            ],
            "suggestion": result.suggestion,
            "cached": info.cached,
            "metadata": {
                "first_release": (
                    info.first_release.astimezone(timezone.utc).isoformat()
                    if info.first_release
                    else None
                ),
                "release_count": info.release_count,
                "repo_url": info.repo_url,
                "github_owner": info.github_owner,
                "github_owner_created": (
                    info.github_owner_created.astimezone(timezone.utc).isoformat()
                    if info.github_owner_created
                    else None
                ),
                "github_signal": (
                    "available" if result.github_signal_available else "unavailable"
                ),
            },
        }

    counts = {level: 0 for level in (LEVEL_LOW, LEVEL_MEDIUM, LEVEL_HIGH, LEVEL_CRITICAL)}
    not_found = errors = 0
    for r in results:
        if r.info.status == STATUS_NOT_FOUND:
            not_found += 1
        elif r.info.status == STATUS_ERROR:
            errors += 1
        elif r.level:
            counts[r.level] += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "gatekeeper", "version": __version__},
        "generated_at": utcnow().isoformat(),
        "results": [one(r) for r in results],
        "summary": {
            "total": len(results),
            "low": counts[LEVEL_LOW],
            "medium": counts[LEVEL_MEDIUM],
            "high": counts[LEVEL_HIGH],
            "critical": counts[LEVEL_CRITICAL],
            "not_found": not_found,
            "errors": errors,
        },
    }


def exit_code(results: list[CheckResult], *, strict: bool = False) -> int:
    """0 = clean; 1 = HIGH/CRITICAL or not-found packages (or, with strict,
    any lookup errors)."""
    for r in results:
        if r.info.status == STATUS_NOT_FOUND:
            return 1
        if r.info.status == STATUS_ERROR and strict:
            return 1
        if r.level in (LEVEL_HIGH, LEVEL_CRITICAL):
            return 1
    return 0
