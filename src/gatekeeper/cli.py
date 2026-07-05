"""Gatekeeper command-line interface (click).

Commands:
    gatekeeper check <package> [--type pip|npm] [--json]
    gatekeeper audit <manifest> [--type pip|npm] [--json] [--output PATH] [--strict]
    gatekeeper cache status
    gatekeeper cache clear
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import os
import sys
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .cache import Cache
from .manifest import ManifestError, load_manifest, write_safe_manifest
from .prefetch import drain, schedule_prefetch
from .registry import RegistryClient
from .report import build_json, exit_code, render_table
from .scoring import CheckResult, evaluate

logger = logging.getLogger("gatekeeper")

_CONCURRENCY = 8


def _make_cache(ctx: click.Context) -> Cache:
    return Cache(path=ctx.obj.get("cache_path"))


def _make_client(cache: Cache) -> RegistryClient:
    return RegistryClient(
        cache=cache,
        github_token=os.environ.get("GITHUB_TOKEN"),
        enable_github=os.environ.get("GATEKEEPER_NO_GITHUB", "") != "1",
    )


def _prefetch_enabled() -> bool:
    return os.environ.get("GATEKEEPER_NO_PREFETCH", "") != "1"


async def _check_names(
    client: RegistryClient, names: list[str], registry: str
) -> list[CheckResult]:
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def one(name: str) -> CheckResult:
        async with semaphore:
            info = await client.fetch_package(name, registry)
        return evaluate(info)

    return list(await asyncio.gather(*(one(n) for n in names)))


def _emit(results: list[CheckResult], as_json: bool, console: Console) -> None:
    if as_json:
        click.echo(jsonlib.dumps(build_json(results), indent=2))
    else:
        render_table(results, console=console)


@click.group()
@click.version_option(version=__version__, prog_name="gatekeeper")
@click.option("--cache-path", type=click.Path(path_type=Path), default=None, hidden=True,
              help="Override cache DB location (mainly for tests).")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, cache_path: Path | None, verbose: bool) -> None:
    """Validate package names against PyPI/npm before installation."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["cache_path"] = cache_path


@cli.command()
@click.argument("package")
@click.option("--type", "pkg_type", type=click.Choice(["pip", "npm"]), default="pip",
              show_default=True, help="Which registry to check against.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def check(ctx: click.Context, package: str, pkg_type: str, as_json: bool) -> None:
    """Check a single PACKAGE name."""
    registry = "pypi" if pkg_type == "pip" else "npm"
    console = Console()

    async def run() -> int:
        with _make_cache(ctx) as cache:
            async with _make_client(cache) as client:
                results = await _check_names(client, [package], registry)
                tasks = (
                    schedule_prefetch(client, [package], registry)
                    if _prefetch_enabled()
                    else []
                )
                # Output first — prefetch never delays the result.
                _emit(results, as_json, console)
                await drain(tasks)
        return exit_code(results)

    sys.exit(asyncio.run(run()))


@cli.command()
@click.argument("manifest", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--type", "pkg_type", type=click.Choice(["pip", "npm"]), default=None,
              help="Override manifest type auto-detection.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--output", type=click.Path(path_type=Path), default=None,
              help="Where to write the cleaned manifest [default: <manifest dir>/manifest.safe].")
@click.option("--strict", is_flag=True, help="Also fail (exit 1) on registry lookup errors.")
@click.pass_context
def audit(
    ctx: click.Context,
    manifest: Path,
    pkg_type: str | None,
    as_json: bool,
    output: Path | None,
    strict: bool,
) -> None:
    """Audit every package in a MANIFEST (requirements.txt or package.json)."""
    console = Console()
    try:
        resolved_type, entries = load_manifest(manifest, pkg_type)
    except (ManifestError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not entries:
        click.echo("No packages found in manifest.", err=True)
        sys.exit(0)

    registry = "pypi" if resolved_type == "pip" else "npm"
    names = [e.name for e in entries]

    async def run() -> int:
        with _make_cache(ctx) as cache:
            async with _make_client(cache) as client:
                results = await _check_names(client, names, registry)
                tasks = (
                    schedule_prefetch(client, names, registry) if _prefetch_enabled() else []
                )
                safe_path, corrections = write_safe_manifest(
                    manifest, resolved_type, results, output
                )
                _emit(results, as_json, console)
                if not as_json:
                    if corrections:
                        fixes = ", ".join(f"{a} -> {b}" for a, b in corrections.items())
                        console.print(f"[green]Corrected in {safe_path.name}:[/green] {fixes}")
                    console.print(f"Cleaned manifest written to [bold]{safe_path}[/bold]")
                await drain(tasks)
        return exit_code(results, strict=strict)

    sys.exit(asyncio.run(run()))


@cli.group()
def cache() -> None:
    """Inspect or clear the local metadata cache."""


@cache.command("status")
@click.pass_context
def cache_status(ctx: click.Context) -> None:
    """Show cache location, entry counts and limits."""
    with _make_cache(ctx) as store:
        info = store.status()
    for key, value in info.items():
        click.echo(f"{key:>18}: {value}")


@cache.command("clear")
@click.pass_context
def cache_clear(ctx: click.Context) -> None:
    """Delete all cached entries (pinned ones will repopulate on next use)."""
    with _make_cache(ctx) as store:
        removed = store.clear()
    click.echo(f"Removed {removed} cached entr{'y' if removed == 1 else 'ies'}.")


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
