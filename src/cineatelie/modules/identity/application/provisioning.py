"""WF-01 — Tenant provisioning (first login) (spec-20260920-backend.md, "WF-01"; BR-ID-01).

Two of the diagram's three branches are implemented:

- **Identity known**: `fn_resolve_user_identity` (ADR-001 addendum) finds the existing
  `user_id` — no provisioning, just resolution.
- **Genuinely new human**: one transaction creates `users`, `user_identities`, `tenants`,
  `memberships` (owner, default), and every per-tenant default row (`tenant_settings`,
  `document_counters` ×4, `card_fees` ×7, `finance_categories` ×10, `contract_templates` ×4,
  a 7-day `subscriptions` trial) — exactly `db/baseline/00001_baseline.sql`'s "Per-tenant
  provisioning template" section, executed as one `UnitOfWork` rather than copy-pasted SQL,
  so the two can never drift.

**Deliberately NOT implemented: "identity unknown, but a human with this verified e-mail
already exists."** BR-ID-01 names this for when a *second* identity provider is added
(Keycloak/Apple/etc. linking to an existing Supabase-created human) — under
`AUTH_PROVIDER=supabase` with Google as the only sign-in method (ADR-006), one physical
person maps to exactly one stable `(provider, subject)` pair, so this branch is currently
unreachable in normal operation. Finding a user by e-mail *before* authentication hits the
same RLS chicken-and-egg gap `fn_resolve_user_identity` was built for (`users_self` is keyed
on `current_user_id()` too) and would need a fourth `SECURITY DEFINER` function — deferred
until a second provider makes the branch reachable, rather than adding that privileged
surface for dead code today. If it is ever hit anyway (e.g. a Supabase user manually deleted
and recreated with the same e-mail), `users_email_unique` still refuses the duplicate and
`ProvisioningConflictError` surfaces it as a clear, diagnosable error instead of either a
silent duplicate or a raw constraint-violation 500.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from cineatelie.core.ids import uuid7
from cineatelie.modules.identity.domain.slug import slugify, with_suffix
from cineatelie.platform.auth.identity_resolution import resolve_user_id
from cineatelie.platform.auth.ports import VerifiedIdentity
from cineatelie.platform.db.uow import UnitOfWork

from .dto import ProvisioningOutcome

logger = logging.getLogger(__name__)

_MAX_SLUG_ATTEMPTS = 5
_TRIAL_DAYS = 7  # must match plans.trial_days for 'trial' (db/seeds/reference.sql)

# [MODELO PADRÃO] Draft-only pt-BR service-agreement text (owner decision 2026-09-27: draft
# now, flagged for legal review before launch, rather than leaving these unwritten or
# fabricating final wording). Referenced by the quote/receipt PDF only as printed boilerplate
# after the quote's own line items and pricing (BR-CTR-06) — no merge fields needed here.
_BODY_SOB_MEDIDA = """CONTRATO DE PRESTAÇÃO DE SERVIÇOS — CONFECÇÃO SOB MEDIDA

1. OBJETO
O presente contrato tem por objeto a confecção sob medida da(s) peça(s) descrita(s) no \
orçamento acima, conforme especificações de modelo, tecido e medidas fornecidas pelo(a) \
cliente.

2. PRAZO
O prazo de entrega é o indicado no orçamento acima e começa a contar a partir da confirmação \
do pedido e do recebimento do sinal, quando houver. Atrasos motivados por caso fortuito, \
força maior ou por alterações solicitadas pelo(a) cliente após o início da confecção poderão \
prorrogar o prazo, mediante aviso prévio.

3. PREÇO E FORMA DE PAGAMENTO
O preço total e as condições de pagamento são os indicados no orçamento acima. O não \
pagamento nas datas acordadas poderá acarretar a suspensão da confecção até a regularização.

4. PROVAS E AJUSTES
O(a) cliente compromete-se a comparecer às provas agendadas nas datas combinadas. O \
reagendamento repetido de provas por parte do(a) cliente poderá impactar o prazo de entrega.

5. GARANTIA
O ateliê garante a correção, sem custo adicional, de eventuais defeitos de confecção \
verificados no ato da entrega ou em até 7 (sete) dias corridos após esta, desde que a peça \
não tenha sido lavada, alterada ou utilizada. A garantia não cobre desgaste natural, mau uso \
ou alterações feitas por terceiros.

6. CANCELAMENTO
Em caso de cancelamento pelo(a) cliente após o início da confecção, o sinal pago não será \
devolvido, servindo como compensação pelos materiais e trabalho já empregados, sem prejuízo \
da cobrança proporcional pelos serviços já executados.

7. RETIRADA
A peça deverá ser retirada em até 30 (trinta) dias corridos após a comunicação de que está \
pronta. Decorrido esse prazo, o ateliê poderá cobrar taxa de guarda ou dispor da peça na \
forma da lei.

8. PROTEÇÃO DE DADOS
As medidas e demais dados pessoais fornecidos são utilizados exclusivamente para a execução \
deste serviço, nos termos da Lei Geral de Proteção de Dados (Lei nº 13.709/2018).

9. FORO
Fica eleito o foro da comarca do domicílio do(a) cliente para dirimir eventuais \
controvérsias decorrentes deste contrato, com renúncia a qualquer outro, por mais \
privilegiado que seja.

[MODELO PADRÃO — revisar com um(a) profissional do direito antes do primeiro uso.]"""

_BODY_AJUSTES = """RECIBO DE SERVIÇOS — AJUSTES E CONSERTOS

1. OBJETO
O presente recibo refere-se à prestação de serviços de ajuste e/ou conserto da(s) peça(s) \
descrita(s) no orçamento acima, conforme especificações indicadas pelo(a) cliente.

2. PRAZO
O prazo de entrega é o indicado no orçamento acima, contado a partir da entrega da peça ao \
ateliê.

3. PREÇO E FORMA DE PAGAMENTO
O preço e a forma de pagamento são os indicados no orçamento acima.

4. RESPONSABILIDADE SOBRE A PEÇA
O ateliê não se responsabiliza por desgastes, manchas ou defeitos preexistentes na peça, não \
identificados no ato do recebimento. Peças confeccionadas em tecidos delicados ou com \
aviamentos frágeis estão sujeitas a risco inerente ao próprio ajuste, o que será informado \
ao(à) cliente sempre que identificável previamente.

5. GARANTIA
O ateliê garante a correção, sem custo adicional, de eventuais defeitos de execução do \
ajuste ou conserto verificados em até 7 (sete) dias corridos após a entrega, desde que a \
peça não tenha sido lavada ou utilizada.

6. RETIRADA
A peça deverá ser retirada em até 30 (trinta) dias corridos após a comunicação de que está \
pronta. Decorrido esse prazo, o ateliê poderá cobrar taxa de guarda ou dispor da peça na \
forma da lei.

7. PROTEÇÃO DE DADOS
Os dados pessoais fornecidos são utilizados exclusivamente para a execução deste serviço, \
nos termos da Lei Geral de Proteção de Dados (Lei nº 13.709/2018).

8. FORO
Fica eleito o foro da comarca do domicílio do(a) cliente para dirimir eventuais \
controvérsias decorrentes deste recibo, com renúncia a qualquer outro, por mais privilegiado \
que seja.

[MODELO PADRÃO — revisar com um(a) profissional do direito antes do primeiro uso.]"""

# document_type -> (title, name, body_text, is_ready). venda/aluguel stay is_ready=False with
# no body_text (source_type stays 'texto' via the CHECK's default path — NULL body_text is
# only valid there because BR-CTR-03 lets the atelier flip is_ready once ITS OWN wording, or
# an uploaded PDF via source_type='arquivo', replaces the row).
_CONTRACT_TEMPLATE_DEFAULTS: list[tuple[str, str, str, str | None, bool]] = [
    (
        "sob_medida",
        "Contrato de prestação de serviços — confecção sob medida",
        "Contrato padrão — sob medida",
        _BODY_SOB_MEDIDA,
        True,
    ),
    (
        "ajuste_conserto",
        "Recibo de serviços — ajustes e consertos",
        "Recibo padrão — ajustes e consertos",
        _BODY_AJUSTES,
        True,
    ),
    (
        "venda",
        "Contrato de compra e venda — peça de pronta entrega",
        "Contrato padrão — venda",
        "[Em desenvolvimento]",
        False,
    ),
    (
        "aluguel",
        "Contrato de locação — peça de pronta entrega",
        "Contrato padrão — aluguel",
        "[Em desenvolvimento]",
        False,
    ),
]

# (document_type, prefix) -- padding defaults to 4 (schema default), matching WF-01's diagram.
_DOCUMENT_COUNTER_DEFAULTS: list[tuple[str, str]] = [
    ("quote", "ORC"),
    ("service_order", "OS"),
    ("receipt", "REC"),
    ("contract", "CTR"),
]

# (installments, fee_pct) -- schema's own "Per-tenant provisioning template" comment.
_CARD_FEE_DEFAULTS: list[tuple[int, str]] = [
    (1, "3.15"),
    (2, "4.79"),
    (3, "6.12"),
    (4, "7.42"),
    (6, "8.80"),
    (10, "12.50"),
    (12, "14.20"),
]

# (name, direction) -- schema's own "Per-tenant provisioning template" comment (10 rows; the
# WF-01 diagram's "8 defaults" undercounts against the schema's actual template, which is the
# more detailed and more recently written source).
_FINANCE_CATEGORY_DEFAULTS: list[tuple[str, str]] = [
    ("Ordem de serviço entregue", "receita"),
    ("Venda de pronta entrega", "receita"),
    ("Aluguel de peça", "receita"),
    ("Compra de material", "despesa"),
    ("Taxas de cartão", "despesa"),
    ("Retirada (pró-labore)", "despesa"),
    ("Manutenção", "despesa"),
    ("Multa por atraso", "receita"),  # rental returned late, BR-RTW-08
    ("Avaria", "receita"),  # rental returned damaged, BR-RTW-08
    ("Assinatura da plataforma", "despesa"),
]


class ProvisioningConflictError(Exception):
    """A human with this verified e-mail already exists under a *different* identity than
    the one being provisioned — see the module docstring's "Deliberately NOT implemented"
    section. `adapters/router.py` maps this to `409` with a support-facing message, since no
    normal signup should ever hit it today."""


def _derive_full_name(identity: VerifiedIdentity) -> str:
    metadata: dict[str, Any] = identity.raw_claims.get("user_metadata", {})
    name = metadata.get("full_name") or metadata.get("name")
    if name:
        return str(name)
    return identity.email.split("@")[0] if identity.email else "Usuário"


def _derive_trade_name(identity: VerifiedIdentity) -> str:
    full_name = _derive_full_name(identity)
    first_name = full_name.split(" ")[0]
    return f"Ateliê {first_name}"


async def provision_or_resolve_user(
    session_factory: async_sessionmaker, identity: VerifiedIdentity, *, provider_name: str
) -> ProvisioningOutcome:
    async with UnitOfWork(session_factory) as uow:
        existing_user_id = await resolve_user_id(
            uow.session, provider=provider_name, provider_subject=identity.subject
        )
    if existing_user_id is not None:
        return ProvisioningOutcome(user_id=existing_user_id, is_new_human=False)

    new_user_id = uuid7()
    new_tenant_id = uuid7()
    full_name = _derive_full_name(identity)
    trade_name = _derive_trade_name(identity)
    base_slug = slugify(trade_name)

    for attempt in range(_MAX_SLUG_ATTEMPTS):
        slug = with_suffix(base_slug, attempt)
        try:
            await _run_provisioning_transaction(
                session_factory,
                user_id=new_user_id,
                tenant_id=new_tenant_id,
                provider_name=provider_name,
                identity=identity,
                full_name=full_name,
                trade_name=trade_name,
                slug=slug,
            )
            return ProvisioningOutcome(user_id=new_user_id, is_new_human=True)
        except IntegrityError as exc:
            # SQLAlchemy's asyncpg DBAPI shim forwards only `.sqlstate`/`.detail`/message
            # from the original error, never `.constraint_name` (confirmed by reading
            # `AsyncAdapt_asyncpg_dbapi.Error.__init__` — it does not exist on the wrapped
            # object, so `getattr(exc.orig, "constraint_name", None)` silently always
            # returns `None`; verified failing that way before switching to this). Postgres's
            # own message embeds the constraint name literally: 'duplicate key value
            # violates unique constraint "tenants_slug_unique"'.
            message = str(exc.orig)
            if "users_email_unique" in message:
                raise ProvisioningConflictError(
                    f"a user with e-mail {identity.email!r} already exists under a "
                    "different identity"
                ) from exc
            if "tenants_slug_unique" in message and attempt < _MAX_SLUG_ATTEMPTS - 1:
                logger.info("slug_collision_retrying", extra={"slug": slug})
                continue
            raise

    raise RuntimeError(f"could not find a free tenant slug after {_MAX_SLUG_ATTEMPTS} attempts")


async def _run_provisioning_transaction(
    session_factory: async_sessionmaker,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    provider_name: str,
    identity: VerifiedIdentity,
    full_name: str,
    trade_name: str,
    slug: str,
) -> None:
    today = date.today()
    trial_ends_on = today + timedelta(days=_TRIAL_DAYS)

    async with UnitOfWork(session_factory, tenant_id=tenant_id, user_id=user_id) as uow:
        session = uow.session

        await session.execute(
            text("INSERT INTO users (id, email, full_name) VALUES (:id, :email, :full_name)"),
            {"id": str(user_id), "email": identity.email, "full_name": full_name},
        )
        await session.execute(
            text(
                "INSERT INTO user_identities "
                "(user_id, provider, provider_subject, email_at_provider, email_verified) "
                "VALUES (:user_id, :provider, :subject, :email, :email_verified)"
            ),
            {
                "user_id": str(user_id),
                "provider": provider_name,
                "subject": identity.subject,
                "email": identity.email,
                "email_verified": identity.email_verified,
            },
        )
        await session.execute(
            text("INSERT INTO tenants (id, slug, trade_name) VALUES (:id, :slug, :trade_name)"),
            {"id": str(tenant_id), "slug": slug, "trade_name": trade_name},
        )
        await session.execute(
            text(
                "INSERT INTO memberships (tenant_id, user_id, role_code, is_default, accepted_at) "
                "VALUES (:tenant_id, :user_id, 'owner', true, now())"
            ),
            {"tenant_id": str(tenant_id), "user_id": str(user_id)},
        )
        await session.execute(
            text("INSERT INTO tenant_settings (tenant_id) VALUES (:tenant_id)"),
            {"tenant_id": str(tenant_id)},
        )

        for document_type, prefix in _DOCUMENT_COUNTER_DEFAULTS:
            await session.execute(
                text(
                    "INSERT INTO document_counters (tenant_id, document_type, prefix) "
                    "VALUES (:tenant_id, :document_type, :prefix)"
                ),
                {"tenant_id": str(tenant_id), "document_type": document_type, "prefix": prefix},
            )

        for installments, fee_pct in _CARD_FEE_DEFAULTS:
            await session.execute(
                text(
                    "INSERT INTO card_fees (tenant_id, installments, fee_pct) "
                    "VALUES (:tenant_id, :installments, :fee_pct)"
                ),
                {"tenant_id": str(tenant_id), "installments": installments, "fee_pct": fee_pct},
            )

        for name, direction in _FINANCE_CATEGORY_DEFAULTS:
            await session.execute(
                text(
                    "INSERT INTO finance_categories (tenant_id, name, direction, is_system) "
                    "VALUES (:tenant_id, :name, :direction, true)"
                ),
                {"tenant_id": str(tenant_id), "name": name, "direction": direction},
            )

        for document_type, title, name, body_text, is_ready in _CONTRACT_TEMPLATE_DEFAULTS:
            await session.execute(
                text(
                    "INSERT INTO contract_templates "
                    "(tenant_id, document_type, title, name, body_text, is_ready) "
                    "VALUES (:tenant_id, :document_type, :title, :name, :body_text, :is_ready)"
                ),
                {
                    "tenant_id": str(tenant_id),
                    "document_type": document_type,
                    "title": title,
                    "name": name,
                    "body_text": body_text,
                    "is_ready": is_ready,
                },
            )

        await session.execute(
            text(
                "INSERT INTO subscriptions "
                "(tenant_id, plan_code, status, starts_on, ends_on, trial_ends_on) "
                "VALUES (:tenant_id, 'trial', 'trialing', :starts_on, :ends_on, :trial_ends_on)"
            ),
            {
                "tenant_id": str(tenant_id),
                "starts_on": today,
                "ends_on": trial_ends_on,
                "trial_ends_on": trial_ends_on,
            },
        )
