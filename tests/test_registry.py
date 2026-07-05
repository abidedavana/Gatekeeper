"""Registry client: parsing, retry/backoff, graceful failure, cache read-through."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp

from gatekeeper.registry import (
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    RegistryClient,
)
from tests.conftest import (
    FakeResponse,
    FakeSession,
    github_user_payload,
    npm_payload,
    pypi_payload,
)


def client_with(session: FakeSession, cache=None, **kwargs) -> RegistryClient:
    kwargs.setdefault("backoff_base", 0.01)
    kwargs.setdefault("enable_github", False)
    return RegistryClient(cache=cache, session=session, **kwargs)


class TestPyPIParsing:
    async def test_ok_package(self):
        created = datetime(2020, 1, 15, tzinfo=timezone.utc)
        session = FakeSession({
            "pypi.org/pypi/goodpkg/json": [
                FakeResponse(200, pypi_payload("goodpkg", first_release=created, versions=7))
            ]
        })
        async with client_with(session) as client:
            info = await client.fetch_package("goodpkg", "pypi")
        assert info.status == STATUS_OK
        assert info.name == "goodpkg"
        assert info.first_release == created
        assert info.release_count == 7

    async def test_github_link_from_project_urls(self):
        session = FakeSession({
            "pypi.org": [
                FakeResponse(200, pypi_payload("g", github="https://github.com/owner/repo"))
            ]
        })
        async with client_with(session) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.repo_url == "https://github.com/owner/repo"


class TestNpmParsing:
    async def test_ok_package(self):
        created = datetime(2019, 6, 1, tzinfo=timezone.utc)
        session = FakeSession({
            "registry.npmjs.org/goodpkg": [
                FakeResponse(200, npm_payload("goodpkg", created=created, versions=12))
            ]
        })
        async with client_with(session) as client:
            info = await client.fetch_package("goodpkg", "npm")
        assert info.status == STATUS_OK
        assert info.first_release == created
        assert info.release_count == 12

    async def test_repository_as_plain_string(self):
        session = FakeSession({
            "registry.npmjs.org": [
                FakeResponse(
                    200,
                    npm_payload("g", github="https://github.com/o/r", repo_as_string=True),
                )
            ]
        })
        async with client_with(session) as client:
            info = await client.fetch_package("g", "npm")
        assert info.repo_url == "https://github.com/o/r"

    async def test_scoped_package_url_encoding(self):
        session = FakeSession({
            "registry.npmjs.org/@aws-sdk%2Fclient-s3": [
                FakeResponse(200, npm_payload("@aws-sdk/client-s3"))
            ]
        })
        async with client_with(session) as client:
            info = await client.fetch_package("@aws-sdk/client-s3", "npm")
        assert info.status == STATUS_OK


class TestFailureModes:
    async def test_404_is_not_found(self):
        session = FakeSession({"pypi.org": [FakeResponse(404, {"message": "Not Found"})]})
        async with client_with(session) as client:
            info = await client.fetch_package("no-such-pkg", "pypi")
        assert info.status == STATUS_NOT_FOUND
        assert len(session.calls) == 1  # 404 is definitive; no retry

    async def test_retry_on_429_then_success(self):
        session = FakeSession({
            "pypi.org": [
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(200, pypi_payload("g")),
            ]
        })
        async with client_with(session) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.status == STATUS_OK
        assert len(session.calls) == 2

    async def test_persistent_500_becomes_error(self):
        session = FakeSession({"pypi.org": [FakeResponse(500)]})
        async with client_with(session, max_retries=3) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.status == STATUS_ERROR
        assert "500" in info.error
        assert len(session.calls) == 3  # exhausted all retries

    async def test_malformed_json_is_error_without_retry(self):
        session = FakeSession({"pypi.org": [FakeResponse(200, json_error=True)]})
        async with client_with(session) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.status == STATUS_ERROR
        assert "JSON" in info.error
        assert len(session.calls) == 1

    async def test_timeout_becomes_error(self):
        session = FakeSession({"pypi.org": [asyncio.TimeoutError()]})
        async with client_with(session, max_retries=2) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.status == STATUS_ERROR
        assert "timeout" in info.error.lower()

    async def test_connection_error_becomes_error(self):
        session = FakeSession({"pypi.org": [aiohttp.ClientConnectionError("refused")]})
        async with client_with(session, max_retries=2) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.status == STATUS_ERROR
        assert "network error" in info.error


class TestCacheIntegration:
    async def test_second_fetch_served_from_cache(self, tmp_cache):
        session = FakeSession({"pypi.org": [FakeResponse(200, pypi_payload("g"))]})
        async with client_with(session, cache=tmp_cache) as client:
            first = await client.fetch_package("g", "pypi")
            second = await client.fetch_package("g", "pypi")
        assert not first.cached
        assert second.cached
        assert second.release_count == first.release_count
        assert len(session.calls) == 1

    async def test_not_found_is_cached(self, tmp_cache):
        session = FakeSession({"pypi.org": [FakeResponse(404, {})]})
        async with client_with(session, cache=tmp_cache) as client:
            await client.fetch_package("ghost", "pypi")
            second = await client.fetch_package("ghost", "pypi")
        assert second.status == STATUS_NOT_FOUND
        assert second.cached
        assert len(session.calls) == 1

    async def test_errors_are_not_cached(self, tmp_cache):
        session = FakeSession({"pypi.org": [FakeResponse(500)]})
        async with client_with(session, cache=tmp_cache, max_retries=1) as client:
            await client.fetch_package("flaky", "pypi")
            await client.fetch_package("flaky", "pypi")
        assert len(session.calls) == 2  # error was retried live, not served from cache


class TestGitHubEnrichment:
    async def test_owner_age_fetched(self):
        owner_created = datetime.now(timezone.utc) - timedelta(days=5)
        session = FakeSession({
            "pypi.org": [
                FakeResponse(200, pypi_payload("g", github="https://github.com/newbie/repo"))
            ],
            "api.github.com/users/newbie": [
                FakeResponse(200, github_user_payload(owner_created))
            ],
        })
        async with client_with(session, enable_github=True) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.github_owner == "newbie"
        assert info.github_owner_created == owner_created
        assert info.github_checked

    async def test_github_failure_leaves_signal_unset(self):
        session = FakeSession({
            "pypi.org": [
                FakeResponse(200, pypi_payload("g", github="https://github.com/x/y"))
            ],
            "api.github.com": [FakeResponse(403, {"message": "rate limited"})],
        })
        async with client_with(session, enable_github=True) as client:
            info = await client.fetch_package("g", "pypi")
        assert info.status == STATUS_OK  # main result unaffected
        assert info.github_owner_created is None

    async def test_no_repo_link_means_no_github_check(self):
        session = FakeSession({"pypi.org": [FakeResponse(200, pypi_payload("g"))]})
        async with client_with(session, enable_github=True) as client:
            info = await client.fetch_package("g", "pypi")
        assert not info.github_checked
        assert info.github_owner_created is None
