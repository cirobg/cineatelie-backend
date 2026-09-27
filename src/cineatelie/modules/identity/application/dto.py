"""Data shapes passed between `application/` and `adapters/` (ADR-004: Pydantic stays in
`adapters/`, so these are plain dataclasses)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProvisioningOutcome:
    user_id: uuid.UUID
    is_new_human: bool


@dataclass(frozen=True, slots=True)
class TenantMembership:
    tenant_id: uuid.UUID
    slug: str
    trade_name: str
    logo_url: str | None
    role_code: str
    is_default: bool
    plan_code: str | None
    subscription_active: bool


@dataclass(frozen=True, slots=True)
class MePayload:
    user_id: uuid.UUID
    email: str
    full_name: str
    avatar_url: str | None
    locale: str
    timezone: str
    tenants: list[TenantMembership] = field(default_factory=list)
