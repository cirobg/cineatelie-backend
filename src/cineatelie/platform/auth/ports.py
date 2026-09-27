"""The `IdentityProvider` port (ADR-006): "one port, `IdentityProvider` — `verify(token)`
and `refresh(refresh_token)` — with a `SupabaseIdentityProvider` adapter." Nothing above
this port may import a provider SDK or know Supabase exists — that is what keeps a future
migration to Keycloak (ADR-006 §"Cutover") an adapter swap instead of a rewrite.

`revoke()` is an addition beyond ADR-006's original two methods, needed by `POST
/auth/logout` (backend spec §4.1: "Revoke the refresh cookie") to also end the session at the
provider — not just stop sending our own cookie — so a stolen refresh token cannot outlive an
explicit logout. Same shape as the other two: one HTTP call to the provider's own endpoint,
nothing SQL-coupled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """What survives token verification. `subject` is the provider's own user id (under
    Supabase, the JWT `sub` — Supabase's id, not Google's; ADR-006 BR-ID-01) and is used only
    as the lookup key into `user_identities`, never as the platform's `user_id`."""

    subject: str
    email: str
    email_verified: bool
    raw_claims: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefreshedSession:
    access_token: str
    refresh_token: str
    expires_in: int


class IdentityProvider(Protocol):
    """Implemented once per identity engine. `modules/identity` depends on this Protocol,
    never on a concrete adapter."""

    async def verify(self, access_token: str) -> VerifiedIdentity:
        """Verify signature, `exp`, `nbf`, `iss` and `aud`. Raises `AppError(code=
        "unauthenticated", status_code=401)` (ADR-006's access-gate table) on any failure —
        expired, malformed, wrong issuer/audience, or an unresolvable signing key."""
        ...

    async def refresh(self, refresh_token: str) -> RefreshedSession:
        """Exchange a refresh token at the provider's own token endpoint. Behind the port
        because the exchange is provider-specific (backend spec `/auth/refresh`)."""
        ...

    async def revoke(self, access_token: str) -> None:
        """Best-effort: end the session at the provider. Callers should not fail the user's
        logout if this raises — clearing the local cookie is the primary guarantee."""
        ...
