from __future__ import annotations

import httpx
import pytest

from cineatelie.platform.auth.jwks import JwksCache, JwksUnavailableError

_SAMPLE_JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "kid": "key-1",
            "use": "sig",
            "alg": "RS256",
            "n": "sXch2Q",
            "e": "AQAB",
        }
    ]
}


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetches_and_caches_keys() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_SAMPLE_JWKS)

    cache = JwksCache(
        jwks_url="https://example/jwks", ttl_s=3600, http_client=_client_with(handler)
    )
    assert not cache.is_ready()

    key = await cache.get_signing_key("key-1")
    assert key.key_id == "key-1"
    assert calls == 1

    # Second call within the TTL must not refetch.
    await cache.get_signing_key("key-1")
    assert calls == 1


async def test_unknown_kid_raises_after_force_refresh_finds_nothing_new() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SAMPLE_JWKS)

    cache = JwksCache(
        jwks_url="https://example/jwks", ttl_s=3600, http_client=_client_with(handler)
    )
    with pytest.raises(JwksUnavailableError):
        await cache.get_signing_key("does-not-exist")


async def test_key_rotation_is_picked_up_by_a_forced_refresh() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_SAMPLE_JWKS)
        return httpx.Response(
            200,
            json={
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": "key-2",
                        "use": "sig",
                        "alg": "RS256",
                        "n": "sXch2Q",
                        "e": "AQAB",
                    }
                ]
            },
        )

    cache = JwksCache(
        jwks_url="https://example/jwks", ttl_s=3600, http_client=_client_with(handler)
    )
    await cache.get_signing_key("key-1")
    assert calls == 1

    # key-1 is still "fresh" by TTL, but rotated out server-side; asking for the new kid
    # must trigger a forced refresh even though the cache hasn't expired.
    key = await cache.get_signing_key("key-2")
    assert key.key_id == "key-2"
    assert calls == 2


async def test_raises_when_cold_and_endpoint_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    cache = JwksCache(
        jwks_url="https://example/jwks", ttl_s=3600, http_client=_client_with(handler)
    )
    with pytest.raises(JwksUnavailableError):
        await cache.get_signing_key("key-1")
    assert not cache.is_ready()


async def test_negative_cache_avoids_a_fetch_storm_while_down() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    cache = JwksCache(
        jwks_url="https://example/jwks", ttl_s=3600, http_client=_client_with(handler)
    )
    for _ in range(5):
        with pytest.raises(JwksUnavailableError):
            await cache.get_signing_key("key-1")

    # Without the negative-cache backoff this would be 5 outbound requests, one per call.
    assert calls == 1


async def test_stale_cache_is_served_when_a_later_refresh_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_SAMPLE_JWKS)
        raise httpx.ConnectError("connection refused", request=request)

    cache = JwksCache(jwks_url="https://example/jwks", ttl_s=0.0, http_client=_client_with(handler))
    await cache.get_signing_key("key-1")

    # TTL is 0, so the next call is stale immediately and attempts a refresh, which fails —
    # the previously cached key must still be returned rather than raising.
    key = await cache.get_signing_key("key-1")
    assert key.key_id == "key-1"
