"""JWKS cache (ADR-006, ADR-009): "JWKS is a network dependency on the request path.
Mitigated by an in-process cache with TTL, a negative-cache guard against fetch storms, and
a readiness probe that fails if the cache is cold and unfetchable."

Fetched with `httpx` (architecture doc's env-var table, `AUTH_JWKS_URL`), never a provider
SDK — this is the one place JWKS wire format is parsed, so a future non-Supabase OIDC
provider only needs a different URL, not different code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt

logger = logging.getLogger(__name__)

# How long a failed fetch is remembered before another one is attempted, while the cache is
# cold. Without this, every request arriving during a provider outage would each trigger its
# own outbound fetch — the exact "fetch storm" ADR-006 calls out. Deliberately independent of
# AUTH_JWKS_CACHE_TTL_S: that TTL governs how long a *good* cache is trusted; this governs how
# often a *bad* one is retried.
_NEGATIVE_CACHE_TTL_S = 10.0


class JwksUnavailableError(Exception):
    """The cache is cold (no previously fetched keys) and the provider could not be reached
    or returned nothing usable. Distinct from "token is invalid" (ADR-006's readiness probe
    must tell these apart)."""


class JwksCache:
    """One instance per process, shared across requests. Not thread-safe across event
    loops — this codebase is single-event-loop async (ADR-003), so an `asyncio.Lock` is
    sufficient to collapse concurrent refreshes into one outbound request."""

    def __init__(self, *, jwks_url: str, ttl_s: float, http_client: httpx.AsyncClient) -> None:
        self._jwks_url = jwks_url
        self._ttl_s = ttl_s
        self._http_client = http_client
        self._keys_by_kid: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float = 0.0
        self._last_attempt_at: float = 0.0
        self._lock = asyncio.Lock()

    def is_ready(self) -> bool:
        """For the process readiness probe (ADR-006): true once at least one successful
        fetch has populated the cache, regardless of whether that fetch has since expired —
        an expired-but-populated cache degrades gracefully (see `_refresh`); only a cache
        that has *never* been filled is a readiness failure."""
        return bool(self._keys_by_kid)

    async def ensure_fetched(self) -> bool:
        """For `/readyz`: nothing else in this process calls `get_signing_key` until the
        first real request carries a token, so a cache that is cold at boot would otherwise
        stay cold — and therefore permanently "not ready" — until then. Triggers one attempt
        and reports the result instead of raising, so a readiness probe can poll this safely."""
        if not self._is_fresh():
            try:
                await self._refresh()
            except JwksUnavailableError:
                pass
        return self.is_ready()

    def _is_fresh(self) -> bool:
        return bool(self._keys_by_kid) and (time.monotonic() - self._fetched_at) < self._ttl_s

    async def get_signing_key(self, kid: str) -> jwt.PyJWK:
        if not self._is_fresh():
            await self._refresh()

        key = self._keys_by_kid.get(kid)
        if key is None:
            # Key rotated since our last successful fetch: one forced refresh before giving
            # up, so a legitimate token signed with a brand-new key isn't rejected just
            # because our cache predates the rotation.
            await self._refresh(force=True)
            key = self._keys_by_kid.get(kid)

        if key is None:
            raise JwksUnavailableError(f"no signing key found for kid={kid!r}")
        return key

    async def _refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and self._is_fresh():
                return  # someone else refreshed while we waited for the lock

            now = time.monotonic()
            if not self._keys_by_kid and (now - self._last_attempt_at) < _NEGATIVE_CACHE_TTL_S:
                raise JwksUnavailableError(
                    "JWKS endpoint recently unreachable; backing off before retrying"
                )
            self._last_attempt_at = now

            try:
                response = await self._http_client.get(self._jwks_url)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if self._keys_by_kid:
                    logger.warning(
                        "jwks_refresh_failed_serving_stale_cache", extra={"error": str(exc)}
                    )
                    return
                raise JwksUnavailableError(f"could not fetch JWKS: {exc}") from exc

            keys_by_kid: dict[str, jwt.PyJWK] = {}
            for raw_key in payload.get("keys", []):
                kid = raw_key.get("kid")
                if kid is None:
                    continue
                keys_by_kid[kid] = jwt.PyJWK.from_dict(raw_key)

            if not keys_by_kid:
                if self._keys_by_kid:
                    logger.warning("jwks_refresh_returned_no_keys_serving_stale_cache")
                    return
                raise JwksUnavailableError("JWKS response contained no usable keys")

            self._keys_by_kid = keys_by_kid
            self._fetched_at = time.monotonic()
