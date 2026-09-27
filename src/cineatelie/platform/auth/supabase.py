"""`IdentityProvider` adapter for Supabase Auth (ADR-006) — the only Supabase-specific code
behind the port. `verify()` checks a JWT's signature and standard claims against the
project's cached JWKS; `refresh()` calls Supabase's own token endpoint, which cannot go
through a generic OIDC client because the grant is provider-specific: Kong (Supabase's API
gateway in front of GoTrue) requires an `apikey` header on every call, including
`/auth/v1/token`, or the request never reaches the auth server at all.

Uses `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (already required by Appendix A for
storage) rather than a new environment variable — the service-role key is already the
project's server-side-only credential, and this call is a server-side call.
"""

from __future__ import annotations

import logging

import httpx
import jwt

from cineatelie.core.errors import AppError
from cineatelie.platform.auth.jwks import JwksCache, JwksUnavailableError
from cineatelie.platform.auth.ports import RefreshedSession, VerifiedIdentity

logger = logging.getLogger(__name__)

# Supabase signs project JWTs asymmetrically (RS256 or ES256 depending on the project's key
# type); neither the legacy shared-secret HS256 mode nor "none" is ever accepted here.
_ALLOWED_ALGORITHMS = ["RS256", "ES256"]


def _unauthenticated(reason: str) -> AppError:
    """ADR-006's access-gate table: a failed JWT is `401 unauthenticated`, always — the
    reason is logged server-side (never in the pt-BR message a client renders)."""
    logger.info("jwt_verification_failed", extra={"reason": reason})
    return AppError(
        code="unauthenticated",
        message="Sua sessão expirou ou é inválida. Faça login novamente.",
        status_code=401,
    )


class SupabaseIdentityProvider:
    """Implements the `IdentityProvider` port (ADR-006) against a Supabase Auth project."""

    def __init__(
        self,
        *,
        jwks_cache: JwksCache,
        issuer: str,
        audience: str,
        supabase_url: str,
        service_role_key: str,
        http_client: httpx.AsyncClient,
        clock_skew_s: float = 30.0,
    ) -> None:
        self._jwks_cache = jwks_cache
        self._issuer = issuer
        self._audience = audience
        self._token_endpoint = f"{supabase_url.rstrip('/')}/auth/v1/token"
        self._logout_endpoint = f"{supabase_url.rstrip('/')}/auth/v1/logout"
        self._service_role_key = service_role_key
        self._http_client = http_client
        self._clock_skew_s = clock_skew_s

    async def verify(self, access_token: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(access_token)
        except jwt.InvalidTokenError as exc:
            raise _unauthenticated(f"malformed token header: {exc}") from exc

        kid = header.get("kid")
        if kid is None:
            raise _unauthenticated("token header has no kid")

        try:
            signing_key = await self._jwks_cache.get_signing_key(kid)
        except JwksUnavailableError as exc:
            raise _unauthenticated(f"jwks unavailable: {exc}") from exc

        try:
            claims = jwt.decode(
                access_token,
                key=signing_key.key,
                algorithms=_ALLOWED_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._clock_skew_s,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise _unauthenticated(str(exc)) from exc

        subject = claims["sub"]
        email = claims.get("email", "")
        # Supabase does not document a single stable location for this across every signup
        # path (top-level `email_verified` vs. `user_metadata.email_verified`, the latter
        # populated by the Google OAuth flow this project actually uses). Checked against a
        # real Google-issued token once Google sign-in is enabled on the project (tracked
        # alongside modules/identity, which is the first consumer of this field).
        email_verified = bool(
            claims.get("email_verified") or claims.get("user_metadata", {}).get("email_verified")
        )

        return VerifiedIdentity(
            subject=subject, email=email, email_verified=email_verified, raw_claims=claims
        )

    async def refresh(self, refresh_token: str) -> RefreshedSession:
        try:
            response = await self._http_client.post(
                self._token_endpoint,
                params={"grant_type": "refresh_token"},
                json={"refresh_token": refresh_token},
                headers={"apikey": self._service_role_key},
            )
        except httpx.HTTPError as exc:
            raise _unauthenticated(f"refresh request failed: {exc}") from exc

        if response.status_code != 200:
            raise _unauthenticated(f"refresh rejected by provider: {response.status_code}")

        payload = response.json()
        try:
            return RefreshedSession(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_in=int(payload["expires_in"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise _unauthenticated(f"malformed refresh response: {exc}") from exc

    async def revoke(self, access_token: str) -> None:
        try:
            response = await self._http_client.post(
                self._logout_endpoint,
                headers={
                    "apikey": self._service_role_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
            if response.status_code not in (204, 200):
                logger.warning(
                    "logout_revoke_rejected_by_provider", extra={"status": response.status_code}
                )
        except httpx.HTTPError as exc:
            logger.warning("logout_revoke_request_failed", extra={"error": str(exc)})
