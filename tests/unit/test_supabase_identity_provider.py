"""Exercises `SupabaseIdentityProvider` against a real RS256-signed JWT and a real JWKS
document generated from the same keypair — not hand-built fixtures — so a mistake in claim
names or verification options (issuer, audience, algorithm allow-list) fails here rather
than only against a live Google login later.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from jwt.algorithms import RSAAlgorithm

from cineatelie.core.errors import AppError
from cineatelie.platform.auth.jwks import JwksCache
from cineatelie.platform.auth.supabase import SupabaseIdentityProvider

_ISSUER = "https://test-ref.supabase.co/auth/v1"
_AUDIENCE = "authenticated"
_KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def jwks_document(rsa_keypair) -> dict:
    _, public_key = rsa_keypair
    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def _make_token(rsa_keypair, *, overrides: dict | None = None, headers: dict | None = None) -> str:
    private_key, _ = rsa_keypair
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": "a1b2c3d4-0000-0000-0000-000000000001",
        "email": "costureira@example.com",
        "user_metadata": {"email_verified": True},
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides or {})
    token_headers = {"kid": _KID}
    token_headers.update(headers or {})
    return jwt.encode(claims, private_key, algorithm="RS256", headers=token_headers)


def _provider(jwks_document: dict, *, refresh_handler=None) -> SupabaseIdentityProvider:
    def jwks_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks_document)

    routes = {"/auth/v1/.well-known/jwks.json": jwks_handler}
    if refresh_handler is not None:
        routes["/auth/v1/token"] = refresh_handler

    def dispatch(request: httpx.Request) -> httpx.Response:
        handler = routes.get(request.url.path)
        assert handler is not None, f"unexpected request to {request.url.path}"
        return handler(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    jwks_cache = JwksCache(
        jwks_url="https://test-ref.supabase.co/auth/v1/.well-known/jwks.json",
        ttl_s=3600,
        http_client=http_client,
    )
    return SupabaseIdentityProvider(
        jwks_cache=jwks_cache,
        issuer=_ISSUER,
        audience=_AUDIENCE,
        supabase_url="https://test-ref.supabase.co",
        service_role_key="sb_secret_test",
        http_client=http_client,
    )


async def test_verify_accepts_a_correctly_signed_token(rsa_keypair, jwks_document) -> None:
    provider = _provider(jwks_document)
    token = _make_token(rsa_keypair)

    identity = await provider.verify(token)

    assert identity.subject == "a1b2c3d4-0000-0000-0000-000000000001"
    assert identity.email == "costureira@example.com"
    assert identity.email_verified is True


async def test_verify_rejects_wrong_issuer(rsa_keypair, jwks_document) -> None:
    provider = _provider(jwks_document)
    token = _make_token(rsa_keypair, overrides={"iss": "https://not-us.supabase.co/auth/v1"})

    with pytest.raises(AppError) as exc_info:
        await provider.verify(token)
    assert exc_info.value.code == "unauthenticated"
    assert exc_info.value.status_code == 401


async def test_verify_rejects_wrong_audience(rsa_keypair, jwks_document) -> None:
    provider = _provider(jwks_document)
    token = _make_token(rsa_keypair, overrides={"aud": "some-other-audience"})

    with pytest.raises(AppError) as exc_info:
        await provider.verify(token)
    assert exc_info.value.code == "unauthenticated"


async def test_verify_rejects_expired_token(rsa_keypair, jwks_document) -> None:
    provider = _provider(jwks_document)
    now = int(time.time())
    token = _make_token(rsa_keypair, overrides={"iat": now - 7200, "exp": now - 3600})

    with pytest.raises(AppError) as exc_info:
        await provider.verify(token)
    assert exc_info.value.code == "unauthenticated"


async def test_verify_rejects_unknown_signing_key(rsa_keypair, jwks_document) -> None:
    provider = _provider(jwks_document)
    token = _make_token(rsa_keypair, headers={"kid": "a-kid-not-in-the-jwks"})

    with pytest.raises(AppError) as exc_info:
        await provider.verify(token)
    assert exc_info.value.code == "unauthenticated"


async def test_verify_rejects_a_token_signed_by_a_different_key(jwks_document) -> None:
    """Same kid as the real key, but signed by an unrelated private key — proves
    verification checks the signature itself, not just that a kid happens to match."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    impostor_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = _provider(jwks_document)
    token = _make_token((impostor_key, None))

    with pytest.raises(AppError) as exc_info:
        await provider.verify(token)
    assert exc_info.value.code == "unauthenticated"


async def test_refresh_returns_new_tokens_on_success(rsa_keypair, jwks_document) -> None:
    def refresh_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "sb_secret_test"
        assert request.url.params["grant_type"] == "refresh_token"
        return httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

    provider = _provider(jwks_document, refresh_handler=refresh_handler)
    result = await provider.refresh("old-refresh-token")

    assert result.access_token == "new-access"
    assert result.refresh_token == "new-refresh"
    assert result.expires_in == 3600


async def test_refresh_raises_unauthenticated_when_provider_rejects_it(
    rsa_keypair, jwks_document
) -> None:
    def refresh_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    provider = _provider(jwks_document, refresh_handler=refresh_handler)

    with pytest.raises(AppError) as exc_info:
        await provider.refresh("expired-or-revoked-token")
    assert exc_info.value.code == "unauthenticated"
    assert exc_info.value.status_code == 401
