"""
Tests for ``storefront_http.afetch_storefront_json``.

Covers the retry policy that keeps a transient storefront rate limit from
aborting a whole ingestion run: which statuses are retried, that the server's
``Retry-After`` hint wins over the exponential backoff, and that
non-retryable statuses fail fast instead of burning the retry budget.
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fashion_bot.services.product_ingestion import storefront_http
from fashion_bot.services.product_ingestion.storefront_http import (
    afetch_storefront_json,
)

URL = "https://example.test/products.json"


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeTransport:
    """Returns a scripted sequence of responses, recording each call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __call__(self, url, params, timeout, host):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return _FakeResponse(200, {"products": []})


@pytest.fixture
def patched(monkeypatch):
    """Install a fake transport and record sleeps instead of waiting."""
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(storefront_http.asyncio, "sleep", _fake_sleep)

    def _install(responses):
        transport = _FakeTransport(responses)
        monkeypatch.setattr(storefront_http, "_get_once", transport)
        return transport, sleeps

    return _install


@pytest.mark.asyncio
async def test_success_returns_payload_without_retrying(patched):
    client, sleeps = patched([_FakeResponse(200, {"products": [{"id": 1}]})])

    result = await afetch_storefront_json(URL)

    assert result == {"products": [{"id": 1}]}
    assert client.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_sends_browser_compatible_user_agent():
    # The default python-httpx agent is an obvious bot signal; the forged TLS
    # fingerprint would be undermined by advertising it.
    assert "python-httpx" not in storefront_http.STOREFRONT_USER_AGENT
    assert storefront_http.STOREFRONT_HEADERS["User-Agent"] == (
        storefront_http.STOREFRONT_USER_AGENT
    )


@pytest.mark.asyncio
async def test_retries_429_then_succeeds(patched):
    client, sleeps = patched([
        _FakeResponse(429),
        _FakeResponse(429),
        _FakeResponse(200, {"products": [{"id": 7}]}),
    ])

    result = await afetch_storefront_json(URL)

    assert result == {"products": [{"id": 7}]}
    assert client.calls == 3
    # Exponential: 2**1 then 2**2.
    assert sleeps == [2.0, 4.0]


@pytest.mark.asyncio
async def test_retries_503(patched):
    client, _ = patched([_FakeResponse(503), _FakeResponse(200, {"products": []})])

    await afetch_storefront_json(URL)

    assert client.calls == 2


@pytest.mark.asyncio
async def test_retry_after_header_overrides_backoff(patched):
    _, sleeps = patched([
        _FakeResponse(429, headers={"retry-after": "7"}),
        _FakeResponse(200, {"products": []}),
    ])

    await afetch_storefront_json(URL)

    assert sleeps == [7.0]


@pytest.mark.asyncio
async def test_absurd_retry_after_is_capped(patched):
    _, sleeps = patched([
        _FakeResponse(429, headers={"retry-after": "99999"}),
        _FakeResponse(200, {"products": []}),
    ])

    await afetch_storefront_json(URL)

    # A misconfigured storefront must not stall onboarding indefinitely.
    assert sleeps == [storefront_http._MAX_RETRY_AFTER]


@pytest.mark.asyncio
async def test_http_date_retry_after_falls_back_to_backoff(patched):
    _, sleeps = patched([
        _FakeResponse(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _FakeResponse(200, {"products": []}),
    ])

    await afetch_storefront_json(URL)

    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_404_fails_fast_without_retrying(patched):
    client, sleeps = patched([_FakeResponse(404)])

    with pytest.raises(httpx.HTTPStatusError):
        await afetch_storefront_json(URL)

    # No amount of waiting turns a 404 into a storefront.
    assert client.calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_exhausted_retries_raise_with_real_status(patched):
    client, sleeps = patched([_FakeResponse(429) for _ in range(4)])

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await afetch_storefront_json(URL, max_retries=4)

    assert client.calls == 4
    assert len(sleeps) == 3  # slept between attempts, not after the last
    assert exc.value.response.status_code == 429


# ── Browser-profile probing ──────────────────────────────────────────────
#
# Cloudflare fingerprints the TLS handshake, so which browser profile is
# accepted varies by storefront. These cover the probe-and-remember logic.


@pytest.fixture
def profiles(monkeypatch):
    """Drive _get_once against a scripted per-profile response map."""
    monkeypatch.setattr(storefront_http, "_CURL_CFFI_AVAILABLE", True)
    monkeypatch.setattr(storefront_http, "_host_profile", {})
    monkeypatch.setattr(
        storefront_http, "DEFAULT_IMPERSONATE_PROFILES", ("alpha", "beta", "gamma")
    )
    monkeypatch.delenv("STOREFRONT_IMPERSONATE_PROFILES", raising=False)

    attempts = []

    def _install(by_profile):
        async def _fake_curl(url, params, timeout, profile):
            attempts.append(profile)
            outcome = by_profile.get(profile, _FakeResponse(403))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(storefront_http, "_get_via_curl_cffi", _fake_curl)
        return attempts

    return _install


@pytest.mark.asyncio
async def test_probes_until_a_profile_is_accepted(profiles):
    attempts = profiles({
        "alpha": _FakeResponse(429),
        "beta": _FakeResponse(200, {"products": [{"id": 1}]}),
    })

    result = await afetch_storefront_json(URL)

    assert result == {"products": [{"id": 1}]}
    assert attempts == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_accepted_profile_is_reused_for_the_host(profiles):
    attempts = profiles({
        "alpha": _FakeResponse(403),
        "beta": _FakeResponse(200, {"products": []}),
    })

    await afetch_storefront_json(URL)
    await afetch_storefront_json(URL)

    # Second call goes straight to the known-good profile — a paginated
    # catalog must not re-probe on every page.
    assert attempts == ["alpha", "beta", "beta"]


@pytest.mark.asyncio
async def test_tls_level_failure_falls_through_to_next_profile(profiles):
    attempts = profiles({
        "alpha": storefront_http.httpx.ConnectError("tls reset"),
        "beta": _FakeResponse(200, {"products": []}),
    })

    await afetch_storefront_json(URL)

    assert attempts == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_all_profiles_rejected_surfaces_the_status(profiles, monkeypatch):
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(storefront_http.asyncio, "sleep", _fake_sleep)
    profiles({p: _FakeResponse(429) for p in ("alpha", "beta", "gamma")})

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await afetch_storefront_json(URL, max_retries=2)

    assert exc.value.response.status_code == 429
    # A host whose profiles all stopped working must re-probe next time
    # rather than stay pinned to a dead one.
    assert storefront_http._host_profile == {}


@pytest.mark.asyncio
async def test_env_var_overrides_profile_list(profiles, monkeypatch):
    monkeypatch.setenv("STOREFRONT_IMPERSONATE_PROFILES", "gamma, beta")
    attempts = profiles({"gamma": _FakeResponse(200, {"products": []})})

    await afetch_storefront_json(URL)

    assert attempts == ["gamma"]
