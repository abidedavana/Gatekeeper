"""Shared fixtures: a fake aiohttp session and registry payload builders.

No test in this suite performs live network I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gatekeeper.cache import Cache


class FakeResponse:
    """Stands in for aiohttp.ClientResponse inside `async with session.get(...)`."""

    def __init__(
        self,
        status: int = 200,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        json_error: bool = False,
    ) -> None:
        self.status = status
        self._json_data = json_data
        self.headers = headers or {}
        self._json_error = json_error

    async def json(self, content_type: str | None = None) -> Any:
        if self._json_error:
            raise ValueError("not json")
        return self._json_data

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RaisingContext:
    """Context manager that raises on entry (simulates transport failures)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Maps URL substrings to queues of FakeResponse (or exceptions to raise).

    Each matching request pops the next scripted item; the last item repeats
    once the queue is exhausted. Records every requested URL in ``calls``.
    """

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def get(self, url: str, headers=None, timeout=None):
        self.calls.append(url)
        for fragment, queue in self.script.items():
            if fragment in url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, BaseException):
                    return _RaisingContext(item)
                return item
        return FakeResponse(status=404, json_data={"message": "Not Found"})

    async def close(self) -> None:
        return None


def pypi_payload(
    name: str = "somepkg",
    *,
    first_release: datetime | None = None,
    versions: int = 5,
    github: str | None = None,
) -> dict:
    """Minimal-but-faithful PyPI JSON API response."""
    first_release = first_release or datetime.now(timezone.utc) - timedelta(days=400)
    releases = {}
    for i in range(versions):
        ts = first_release + timedelta(days=30 * i)
        releases[f"1.{i}.0"] = [
            {"upload_time_iso_8601": ts.isoformat().replace("+00:00", "Z")}
        ]
    info: dict[str, Any] = {"name": name, "project_urls": {}, "home_page": None}
    if github:
        info["project_urls"] = {"Source": github}
    return {"info": info, "releases": releases}


def npm_payload(
    name: str = "somepkg",
    *,
    created: datetime | None = None,
    versions: int = 5,
    github: str | None = None,
    repo_as_string: bool = False,
) -> dict:
    """Minimal-but-faithful npm registry response."""
    created = created or datetime.now(timezone.utc) - timedelta(days=400)
    payload: dict[str, Any] = {
        "name": name,
        "time": {"created": created.isoformat().replace("+00:00", "Z")},
        "versions": {f"1.{i}.0": {} for i in range(versions)},
    }
    if github:
        payload["repository"] = github if repo_as_string else {"type": "git", "url": github}
    return payload


def github_user_payload(created: datetime | None = None) -> dict:
    created = created or datetime.now(timezone.utc) - timedelta(days=2000)
    return {"login": "someone", "created_at": created.isoformat().replace("+00:00", "Z")}


@pytest.fixture
def tmp_cache(tmp_path):
    cache = Cache(path=tmp_path / "cache.db")
    yield cache
    cache.close()
