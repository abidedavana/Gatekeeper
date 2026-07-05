"""Async clients for the PyPI JSON API, the npm registry API, and (best-effort)
the GitHub users API.

Exact JSON fields consumed (verified against the live response schemas):

PyPI  ``GET https://pypi.org/pypi/<name>/json``
    * ``info.name``                       — canonical project name
    * ``info.project_urls`` / ``info.home_page`` — scanned for a GitHub link
    * ``releases`` — mapping of version -> list of file dicts; each file dict
      has ``upload_time_iso_8601``. First-release date = the minimum
      ``upload_time_iso_8601`` across all files; release count =
      number of keys in ``releases``.

npm   ``GET https://registry.npmjs.org/<name>``
    * ``name``
    * ``time.created``      — first publish timestamp (ISO 8601)
    * ``versions``          — mapping of version -> manifest; release count = len()
    * ``repository.url``    — scanned for a GitHub link (``repository`` may
      also be a plain string on older packages; both forms are handled)

GitHub ``GET https://api.github.com/users/<owner>``
    * ``created_at``        — account creation date (works for orgs too)

Neither PyPI nor npm exposes maintainer account-creation dates through these
public endpoints, so that signal is only available via the GitHub fallback and
is reported as unavailable when a package links no GitHub repository.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/{name}/json"
NPM_URL = "https://registry.npmjs.org/{name}"
GITHUB_USER_URL = "https://api.github.com/users/{owner}"

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_GITHUB_RE = re.compile(r"github\.com[/:]([A-Za-z0-9][A-Za-z0-9-]*)(?:/|$)", re.IGNORECASE)

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"


def _parse_iso(value: str) -> datetime | None:
    try:
        # Python 3.10's fromisoformat does not accept a trailing 'Z'.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@dataclass
class PackageInfo:
    """Normalized metadata for one package from one registry."""

    name: str
    registry: str  # "pypi" | "npm"
    status: str  # STATUS_OK | STATUS_NOT_FOUND | STATUS_ERROR
    error: str | None = None
    first_release: datetime | None = None
    release_count: int | None = None
    repo_url: str | None = None
    github_owner: str | None = None
    github_owner_created: datetime | None = None
    github_checked: bool = False  # True once the GitHub lookup was attempted
    cached: bool = False

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "registry": self.registry,
            "status": self.status,
            "first_release": self.first_release.isoformat() if self.first_release else None,
            "release_count": self.release_count,
            "repo_url": self.repo_url,
            "github_owner": self.github_owner,
            "github_owner_created": (
                self.github_owner_created.isoformat() if self.github_owner_created else None
            ),
            "github_checked": self.github_checked,
        }

    @classmethod
    def from_cache_payload(cls, payload: dict[str, Any]) -> PackageInfo:
        return cls(
            name=payload["name"],
            registry=payload["registry"],
            status=payload["status"],
            first_release=_parse_iso(payload["first_release"]) if payload["first_release"] else None,
            release_count=payload["release_count"],
            repo_url=payload.get("repo_url"),
            github_owner=payload.get("github_owner"),
            github_owner_created=(
                _parse_iso(payload["github_owner_created"])
                if payload.get("github_owner_created")
                else None
            ),
            github_checked=payload.get("github_checked", False),
            cached=True,
        )


@dataclass
class FetchOutcome:
    """Result of one HTTP GET: exactly one of (data, error) is meaningful."""

    status_code: int | None
    data: Any | None = None
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class RegistryClient:
    """Fetches and normalizes package metadata, with cache read-through."""

    def __init__(
        self,
        cache=None,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        github_token: str | None = None,
        enable_github: bool = True,
    ) -> None:
        self.cache = cache
        self._session = session
        self._owns_session = session is None
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.github_token = github_token
        self.enable_github = enable_github

    async def __aenter__(self) -> RegistryClient:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "gatekeeper-cli"},
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # -- low-level HTTP with retry/backoff ----------------------------------

    async def _get_json(self, url: str, headers: dict[str, str] | None = None) -> FetchOutcome:
        """GET *url*; retry with exponential backoff on 429/5xx and transport
        errors. 4xx other than 429 and malformed JSON are returned immediately
        (retrying would not help)."""
        assert self._session is not None, "RegistryClient must be used as an async context manager"
        last: FetchOutcome = FetchOutcome(None, error="no request attempted")
        for attempt in range(self.max_retries):
            try:
                async with self._session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    if resp.status in RETRYABLE_STATUSES:
                        last = FetchOutcome(
                            resp.status, error=f"HTTP {resp.status}", headers=dict(resp.headers)
                        )
                    else:
                        try:
                            data = await resp.json(content_type=None)
                        except (ValueError, aiohttp.ClientError):
                            return FetchOutcome(resp.status, error="malformed JSON response")
                        return FetchOutcome(resp.status, data=data, headers=dict(resp.headers))
            except asyncio.TimeoutError:
                last = FetchOutcome(None, error=f"timeout after {self.timeout}s")
            except aiohttp.ClientError as exc:
                last = FetchOutcome(None, error=f"network error: {exc}")

            if attempt < self.max_retries - 1:
                delay = self.backoff_base * (2**attempt)
                retry_after = last.headers.get("Retry-After") if last.headers else None
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                logger.debug("retrying %s in %.1fs (%s)", url, delay, last.error)
                await asyncio.sleep(delay)
        return last

    # -- registry-specific parsing -------------------------------------------

    @staticmethod
    def _parse_pypi(name: str, data: dict[str, Any]) -> PackageInfo:
        info = data.get("info") or {}
        releases = data.get("releases") or {}
        upload_times = [
            _parse_iso(f["upload_time_iso_8601"])
            for files in releases.values()
            for f in files
            if f.get("upload_time_iso_8601")
        ]
        upload_times = [t for t in upload_times if t is not None]

        repo_url = None
        candidates = list((info.get("project_urls") or {}).values())
        if info.get("home_page"):
            candidates.append(info["home_page"])
        for url in candidates:
            if url and "github.com" in url.lower():
                repo_url = url
                break

        return PackageInfo(
            name=info.get("name", name),
            registry="pypi",
            status=STATUS_OK,
            first_release=min(upload_times) if upload_times else None,
            release_count=len(releases),
            repo_url=repo_url,
        )

    @staticmethod
    def _parse_npm(name: str, data: dict[str, Any]) -> PackageInfo:
        times = data.get("time") or {}
        repo = data.get("repository")
        repo_url = None
        if isinstance(repo, dict):
            repo_url = repo.get("url")
        elif isinstance(repo, str):
            repo_url = repo
        if repo_url and "github.com" not in repo_url.lower():
            repo_url = None

        return PackageInfo(
            name=data.get("name", name),
            registry="npm",
            status=STATUS_OK,
            first_release=_parse_iso(times["created"]) if times.get("created") else None,
            release_count=len(data.get("versions") or {}),
            repo_url=repo_url,
        )

    # -- public API ------------------------------------------------------------

    async def fetch_package(self, name: str, registry: str) -> PackageInfo:
        """Fetch (or read from cache) normalized metadata for one package.

        Never raises for network/registry problems: failures come back as a
        PackageInfo with status='error' and a human-readable message.
        """
        if self.cache is not None:
            try:
                hit = self.cache.get(registry, name)
            except Exception as exc:  # cache trouble must not break checks
                logger.warning("cache read failed for %s/%s: %s", registry, name, exc)
                hit = None
            if hit is not None:
                return PackageInfo.from_cache_payload(hit)

        url = (PYPI_URL if registry == "pypi" else NPM_URL).format(name=quote(name, safe="@"))
        outcome = await self._get_json(url)

        if outcome.status_code == 404:
            info = PackageInfo(name=name, registry=registry, status=STATUS_NOT_FOUND)
        elif outcome.error is not None or not isinstance(outcome.data, dict):
            info = PackageInfo(
                name=name,
                registry=registry,
                status=STATUS_ERROR,
                error=outcome.error or "unexpected response shape",
            )
        else:
            try:
                if registry == "pypi":
                    info = self._parse_pypi(name, outcome.data)
                else:
                    info = self._parse_npm(name, outcome.data)
            except Exception as exc:
                logger.warning("failed to parse %s response for %s: %s", registry, name, exc)
                info = PackageInfo(
                    name=name, registry=registry, status=STATUS_ERROR,
                    error=f"unparseable registry response: {exc}",
                )

        if info.status == STATUS_OK and info.repo_url and self.enable_github:
            await self._enrich_github(info)

        # Cache successes and not-found (both are stable facts for 24h);
        # never cache transient errors.
        if self.cache is not None and info.status != STATUS_ERROR:
            try:
                self.cache.put(registry, name, info.to_cache_payload())
            except Exception as exc:
                logger.warning("cache write failed for %s/%s: %s", registry, name, exc)
        return info

    async def _enrich_github(self, info: PackageInfo) -> None:
        """Best-effort GitHub owner account age. Failures leave the signal unset."""
        match = _GITHUB_RE.search(info.repo_url or "")
        if not match:
            return
        info.github_owner = match.group(1)
        info.github_checked = True
        headers = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        try:
            outcome = await self._get_json(
                GITHUB_USER_URL.format(owner=quote(info.github_owner)), headers=headers
            )
            if outcome.error is None and isinstance(outcome.data, dict):
                created = outcome.data.get("created_at")
                if created:
                    info.github_owner_created = _parse_iso(created)
        except Exception as exc:
            logger.debug("github lookup failed for %s: %s", info.github_owner, exc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
