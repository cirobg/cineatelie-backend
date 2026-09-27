"""Wire schemas (ADR-004: Pydantic stays in `adapters/`, never `domain/`)."""

from __future__ import annotations

import dataclasses
import uuid

from pydantic import BaseModel

from cineatelie.modules.identity.application.dto import MePayload


class ExchangeRequest(BaseModel):
    access_token: str
    refresh_token: str


class TenantMembershipOut(BaseModel):
    tenant_id: uuid.UUID
    slug: str
    trade_name: str
    logo_url: str | None
    role_code: str
    is_default: bool
    plan_code: str | None
    subscription_active: bool


class MeOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    avatar_url: str | None
    locale: str
    timezone: str
    tenants: list[TenantMembershipOut]

    @classmethod
    def from_payload(cls, payload: MePayload) -> MeOut:
        return cls(
            user_id=payload.user_id,
            email=payload.email,
            full_name=payload.full_name,
            avatar_url=payload.avatar_url,
            locale=payload.locale,
            timezone=payload.timezone,
            tenants=[TenantMembershipOut(**dataclasses.asdict(t)) for t in payload.tenants],
        )


class ExchangeResponse(MeOut):
    access_token: str


class RefreshResponse(BaseModel):
    access_token: str


class PatchMeRequest(BaseModel):
    full_name: str | None = None
    avatar_url: str | None = None
    timezone: str | None = None


class PermissionsResponse(BaseModel):
    permissions: list[str]
