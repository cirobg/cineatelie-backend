-- =====================================================================================
-- Cine Ateliê — SaaS for couture ateliers
-- PostgreSQL 15+ physical schema (baseline / v0.1.0)
-- =====================================================================================
-- Conventions
--   * All identifiers, comments and code are in English. End-user facing STRINGS stored
--     as reference/seed data are in pt-BR, because the UI is pt-BR (see spec docs).
--   * Primary keys: UUIDv7 (time-ordered) generated application-side or by cineatelie.uuid_generate_v7().
--   * Money: NUMERIC(14,2). Quantities: NUMERIC(14,3). Percentages: NUMERIC(7,4).
--   * Timestamps: TIMESTAMPTZ, always UTC. Business dates: DATE (tenant timezone applied
--     by the backend before persisting).
--   * Every business table carries tenant_id and is protected by Row-Level Security.
--   * Status/enum-like columns are TEXT + CHECK (see ADR-008) to keep migrations cheap.
--   * Soft delete via deleted_at on entities the user can "remove" from the UI.
-- Reference: 02-architecture/architecture-20260920-cineatelie.md
--            03-spec/spec-20260920-database.md
-- =====================================================================================

BEGIN;

-- -------------------------------------------------------------------------------------
-- 0. Extensions & shared helpers
-- -------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS cineatelie;

-- Everything below — extensions included — resolves into this schema by default for the
-- rest of this script (this SET is session-scoped, not persisted — see the per-role SET
-- further down for what makes it stick for every future connection as
-- app_user/app_worker/app_migrator). The schema must exist and be on the search_path
-- *before* CREATE EXTENSION runs, or citext/pgcrypto/btree_gist install into `public`
-- instead — found by actually running this against a live Postgres: with `public` no
-- longer on the path, `citext NOT NULL` on the very first table failed with
-- "type citext does not exist", because the type had been created a schema away.
--
-- `extensions` is a second, fallback entry, not a second place of our own choosing:
-- Supabase pre-installs `pgcrypto` in a schema literally called `extensions` before this
-- baseline ever runs, so `CREATE EXTENSION IF NOT EXISTS pgcrypto` below is a silent no-op
-- there — it already exists, just not where we assumed. Without `extensions` on the path,
-- `cineatelie.uuid_generate_v7()`'s call to `gen_random_bytes()` fails with "function does
-- not exist" on Supabase specifically, while working fine locally (plain `postgres:15` has
-- no pre-installed pgcrypto, so our own `CREATE EXTENSION` truly creates it, in
-- `cineatelie`, where the unqualified call already resolves). Listing `extensions` here
-- costs nothing where it doesn't exist — an absent schema in `search_path` is silently
-- skipped, never an error — and makes the same baseline correct on both.
SET search_path = cineatelie, extensions;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_bytes, digest, PGP column encryption
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive e-mail
CREATE EXTENSION IF NOT EXISTS btree_gist; -- exclusion constraints (subscription windows)

-- UUIDv7 (RFC 9562) generator. Kept in the DB so that manual inserts / migrations
-- produce the same key shape the application does. See ADR-002.
CREATE OR REPLACE FUNCTION cineatelie.uuid_generate_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $fn$
DECLARE
    unix_ts_ms  bigint;
    rand_bytes  bytea;
    uuid_bytes  bytea;
BEGIN
    unix_ts_ms := (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint;
    rand_bytes := gen_random_bytes(10);
    uuid_bytes := set_byte(set_byte(set_byte(set_byte(set_byte(set_byte(
                    ('\x000000000000'::bytea || rand_bytes),
                    0, ((unix_ts_ms >> 40) & 255)::int),
                    1, ((unix_ts_ms >> 32) & 255)::int),
                    2, ((unix_ts_ms >> 24) & 255)::int),
                    3, ((unix_ts_ms >> 16) & 255)::int),
                    4, ((unix_ts_ms >>  8) & 255)::int),
                    5, ( unix_ts_ms        & 255)::int);
    -- version 7
    uuid_bytes := set_byte(uuid_bytes, 6, ((get_byte(uuid_bytes, 6) & 15) | 112));
    -- IETF variant
    uuid_bytes := set_byte(uuid_bytes, 8, ((get_byte(uuid_bytes, 8) & 63) | 128));
    RETURN encode(uuid_bytes, 'hex')::uuid;
END;
$fn$;

-- Current tenant from the request-scoped GUC set by the API middleware
-- (SET LOCAL app.current_tenant_id = '<uuid>'). Returns NULL when unset.
CREATE OR REPLACE FUNCTION cineatelie.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $fn$
    SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid;
$fn$;

CREATE OR REPLACE FUNCTION cineatelie.current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $fn$
    SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid;
$fn$;

-- Generic updated_at trigger
CREATE OR REPLACE FUNCTION cineatelie.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$fn$;

-- Guard: a row must never be written with a tenant_id different from the session tenant.
-- RLS already enforces this; the trigger produces a clearer error and also covers
-- maintenance sessions that forget to scope.
CREATE OR REPLACE FUNCTION cineatelie.assert_tenant_matches()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF cineatelie.current_tenant_id() IS NOT NULL AND NEW.tenant_id IS DISTINCT FROM cineatelie.current_tenant_id() THEN
        RAISE EXCEPTION 'tenant_id mismatch: row=% session=%', NEW.tenant_id, cineatelie.current_tenant_id()
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$fn$;

-- Append-only guard, strict. Used for financial ledgers that must never be pruned.
CREATE OR REPLACE FUNCTION cineatelie.forbid_write()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION '% is append-only; correct it with a compensating row instead', TG_TABLE_NAME
        USING ERRCODE = '42501';
END;
$fn$;

-- Append-only guard with a retention exception. UPDATE is always rejected; DELETE is
-- permitted only while the session has opted in with
--   SET LOCAL app.retention_sweep = 'on'
-- which only the scheduled retention job does. Without this, the append-only trigger
-- would block the LGPD retention sweep on audit_log.
CREATE OR REPLACE FUNCTION cineatelie.append_only_with_retention()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF TG_OP = 'DELETE'
       AND COALESCE(current_setting('app.retention_sweep', true), 'off') = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        '% is append-only; DELETE is allowed only inside a retention sweep', TG_TABLE_NAME
        USING ERRCODE = '42501';
END;
$fn$;

-- -------------------------------------------------------------------------------------
-- 1. Identity (tenant-agnostic)
-- -------------------------------------------------------------------------------------

-- A human being. Decoupled from any authentication provider (ADR-005).
CREATE TABLE users (
    id                uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    email             citext NOT NULL,
    full_name         text NOT NULL,
    avatar_url        text,
    locale            text NOT NULL DEFAULT 'pt-BR',
    timezone          text NOT NULL DEFAULT 'America/Sao_Paulo',
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','suspended','deleted')),
    last_login_at     timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz,
    CONSTRAINT users_email_unique UNIQUE (email)
);
COMMENT ON TABLE users IS
  'Global human identity. Never the owner of business data - that is tenant_id (ADR-001).';

-- One row per (provider, subject). Enables adding providers or migrating the auth
-- engine (Supabase Auth -> Ory/Keycloak) without touching domain tables.
CREATE TABLE user_identities (
    id                 uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider           text NOT NULL CHECK (provider IN ('google','supabase','keycloak','ory','apple')),
    provider_subject   text NOT NULL,          -- e.g. the immutable Google "sub"
    email_at_provider  citext NOT NULL,
    email_verified     boolean NOT NULL DEFAULT false,
    raw_profile        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_seen_at       timestamptz,
    CONSTRAINT user_identities_provider_subject_unique UNIQUE (provider, provider_subject)
);
CREATE INDEX user_identities_user_idx ON user_identities (user_id);

-- -------------------------------------------------------------------------------------
-- 2. Tenancy, roles and membership
-- -------------------------------------------------------------------------------------

CREATE TABLE tenants (
    id                 uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    slug               text NOT NULL,
    trade_name         text NOT NULL,                 -- "Cine Ateliê"
    legal_name         text,
    cnpj_ciphertext    bytea,                         -- pgcrypto / Vault, see ADR-013
    cnpj_last4         text,                          -- for UI display and lookup
    instagram_handle   text,
    review_url         text,
    website_url        text,
    email              citext,
    phone              text,
    logo_url           text,
    pix_qr_url         text,
    pix_key_ciphertext bytea,
    address_line       text,
    city               text,
    state_code         text,
    postal_code        text,
    country_code       text NOT NULL DEFAULT 'BR',
    timezone           text NOT NULL DEFAULT 'America/Sao_Paulo',
    currency_code      text NOT NULL DEFAULT 'BRL',
    locale             text NOT NULL DEFAULT 'pt-BR',
    status             text NOT NULL DEFAULT 'active'
                       CHECK (status IN ('provisioning','active','suspended','closed')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    CONSTRAINT tenants_slug_unique UNIQUE (slug),
    CONSTRAINT tenants_slug_format CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$')
);
COMMENT ON TABLE tenants IS 'The atelier workspace. Owner of ALL business data via tenant_id.';

CREATE TABLE roles (
    code         text PRIMARY KEY,          -- owner, finance, seamstress, front_desk
    label_pt_br  text NOT NULL,             -- Proprietária, Financeiro, Costureira, Atendimento
    description  text,
    is_system    boolean NOT NULL DEFAULT true,
    sort_order   smallint NOT NULL DEFAULT 0
);

CREATE TABLE permissions (
    code         text PRIMARY KEY,          -- e.g. quotes:write
    resource     text NOT NULL,
    action       text NOT NULL CHECK (action IN ('read','write','delete','manage')),
    description  text
);

CREATE TABLE role_permissions (
    role_code        text NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    permission_code  text NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_code, permission_code)
);

CREATE TABLE memberships (
    id            uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_code     text NOT NULL REFERENCES roles(code),
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('invited','active','suspended','revoked')),
    is_default    boolean NOT NULL DEFAULT false,  -- tenant pre-selected at login
    invited_by    uuid REFERENCES users(id),
    invited_at    timestamptz,
    accepted_at   timestamptz,
    revoked_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memberships_tenant_user_unique UNIQUE (tenant_id, user_id)
);
CREATE INDEX memberships_user_idx   ON memberships (user_id)   WHERE status = 'active';
CREATE INDEX memberships_tenant_idx ON memberships (tenant_id) WHERE status = 'active';
-- At least one active owner must remain per tenant; enforced by the application service
-- layer, and this partial index makes the check a single index probe.
CREATE INDEX memberships_owner_idx  ON memberships (tenant_id)
    WHERE role_code = 'owner' AND status = 'active';

CREATE TABLE tenant_invitations (
    id            uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email         citext NOT NULL,
    role_code     text NOT NULL REFERENCES roles(code),
    token_hash    bytea NOT NULL,            -- sha256 of the opaque invite token
    expires_at    timestamptz NOT NULL,
    invited_by    uuid NOT NULL REFERENCES users(id),
    accepted_at   timestamptz,
    accepted_by   uuid REFERENCES users(id),
    revoked_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_invitations_token_unique UNIQUE (token_hash)
);
CREATE INDEX tenant_invitations_pending_idx
    ON tenant_invitations (tenant_id, email) WHERE accepted_at IS NULL AND revoked_at IS NULL;

-- -------------------------------------------------------------------------------------
-- 3. Plans, subscriptions and entitlements (ADR-006)
-- -------------------------------------------------------------------------------------

CREATE TABLE features (
    key          text PRIMARY KEY,          -- quotes, orders, finance, courses, whatsapp...
    label_pt_br  text NOT NULL,
    description  text,
    is_active    boolean NOT NULL DEFAULT true
);

CREATE TABLE plans (
    code             text PRIMARY KEY,      -- trial, starter, pro
    label_pt_br      text NOT NULL,
    description      text,
    price_amount     numeric(14,2) NOT NULL DEFAULT 0,
    currency_code    text NOT NULL DEFAULT 'BRL',
    billing_period   text NOT NULL DEFAULT 'annual'
                     CHECK (billing_period IN ('monthly','annual','lifetime')),
    trial_days       smallint NOT NULL DEFAULT 0,
    is_public        boolean NOT NULL DEFAULT true,
    is_active        boolean NOT NULL DEFAULT true,
    sort_order       smallint NOT NULL DEFAULT 0
);

CREATE TABLE plan_features (
    plan_code    text NOT NULL REFERENCES plans(code) ON DELETE CASCADE,
    feature_key  text NOT NULL REFERENCES features(key) ON DELETE CASCADE,
    is_enabled   boolean NOT NULL DEFAULT true,
    limit_value  integer,                   -- NULL = unlimited
    PRIMARY KEY (plan_code, feature_key)
);

-- A tenant's entitlement window. History is kept; at most one row may be active on a
-- given day, enforced by the exclusion constraint.
CREATE TABLE subscriptions (
    id                       uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id                uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_code                text NOT NULL REFERENCES plans(code),
    status                   text NOT NULL DEFAULT 'active'
                             CHECK (status IN ('trialing','active','past_due','cancelled','expired')),
    starts_on                date NOT NULL,
    ends_on                  date NOT NULL,           -- inclusive; access blocked after this date
    trial_ends_on            date,
    grace_days               smallint NOT NULL DEFAULT 0,
    -- Price SNAPSHOT taken when this subscription was sold. plans.price_amount may be
    -- changed at any time; this column keeps what THIS tenant actually agreed to pay, so
    -- repricing never rewrites history. Same pattern as quotes.card_fee_pct.
    price_amount             numeric(14,2),
    currency_code            text NOT NULL DEFAULT 'BRL',
    -- What happens to the price at RENEWAL. A renewal always creates a NEW subscriptions
    -- row; this flag decides the price that row is given:
    --   false (default) -> renew at the CURRENT plans.price_amount. The old price applied
    --                      for the term that was paid for, and no longer.
    --   true            -> copy price_amount forward indefinitely. A permanent price lock,
    --                      for founding customers or a negotiated rate.
    -- The flag itself is copied to the renewal row, so a lock persists until cleared.
    -- Without this column "grandfathering" would be ambiguous at renewal (BR-SUB-02).
    price_locked             boolean NOT NULL DEFAULT false,
    cancel_requested_at      timestamptz,
    payment_provider         text CHECK (payment_provider IN ('manual','stripe','asaas','mercadopago')),
    external_customer_id     text,
    external_subscription_id text,
    notes                    text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT subscriptions_period_valid CHECK (ends_on >= starts_on),
    CONSTRAINT subscriptions_no_overlap
        EXCLUDE USING gist (
            tenant_id WITH =,
            daterange(starts_on, ends_on, '[]') WITH &&
        ) WHERE (status IN ('trialing','active','past_due'))
);
CREATE INDEX subscriptions_tenant_window_idx ON subscriptions (tenant_id, ends_on DESC);

-- Per-tenant overrides on top of the plan (beta flags, goodwill grants).
-- =====================================================================================
-- Quotas — numeric ceilings, as distinct from features (on/off).
-- =====================================================================================
-- `features` answers "can this tenant use attachments at all?"; `quotas` answers
-- "how many, and how much disk?". Keeping them apart avoids overloading
-- plan_features.limit_value, which can only carry one number per feature.
--
-- These exist because a QUOTA is a better cost control than a retention sweep: a ceiling
-- is a forward-looking rule the customer accepts at signup, whereas deleting their files
-- later is a loss they experience as betrayal. The quota bounds spend; retention only
-- satisfies LGPD (owner decision, 2026-09-26).
--
-- Resolution order, most specific first:
--     tenant_quota_overrides  ->  plan_quotas  ->  absent = unlimited
CREATE TABLE quotas (
    key          text PRIMARY KEY,   -- attachments_per_item, attachments_total, max_attachment_mb, storage_mb_total
    label_pt_br  text NOT NULL,
    unit         text NOT NULL CHECK (unit IN ('count','megabytes')),
    description  text
);

CREATE TABLE plan_quotas (
    plan_code   text NOT NULL REFERENCES plans(code) ON DELETE CASCADE,
    quota_key   text NOT NULL REFERENCES quotas(key) ON DELETE CASCADE,
    limit_value integer CHECK (limit_value IS NULL OR limit_value >= 0),  -- NULL = unlimited
    PRIMARY KEY (plan_code, quota_key)
);

-- Per-tenant grant above (or below) the plan. Same shape as tenant_feature_overrides.
CREATE TABLE tenant_quota_overrides (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    quota_key   text NOT NULL REFERENCES quotas(key) ON DELETE CASCADE,
    limit_value integer CHECK (limit_value IS NULL OR limit_value >= 0),
    expires_on  date,
    reason      text,
    PRIMARY KEY (tenant_id, quota_key)
);

CREATE TABLE tenant_feature_overrides (
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    feature_key  text NOT NULL REFERENCES features(key) ON DELETE CASCADE,
    is_enabled   boolean NOT NULL,
    limit_value  integer,
    expires_on   date,
    reason       text,
    PRIMARY KEY (tenant_id, feature_key)
);

CREATE TABLE billing_invoices (
    id               uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    subscription_id  uuid REFERENCES subscriptions(id) ON DELETE SET NULL,
    provider         text NOT NULL,
    external_id      text,
    amount           numeric(14,2) NOT NULL,
    currency_code    text NOT NULL DEFAULT 'BRL',
    status           text NOT NULL CHECK (status IN ('draft','open','paid','void','uncollectible')),
    issued_on        date NOT NULL,
    due_on           date,
    paid_at          timestamptz,
    hosted_url       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_invoices_external_unique UNIQUE (provider, external_id)
);
CREATE INDEX billing_invoices_tenant_idx ON billing_invoices (tenant_id, issued_on DESC);

-- Raw inbound webhooks, stored before parsing, for idempotency and audit (ADR-014).
CREATE TABLE webhook_events (
    id                   uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    provider             text NOT NULL,
    external_event_id    text NOT NULL,
    event_type           text,
    signature_verified   boolean NOT NULL DEFAULT false,
    payload              jsonb NOT NULL,
    received_at          timestamptz NOT NULL DEFAULT now(),
    processed_at         timestamptz,
    processing_status    text NOT NULL DEFAULT 'pending'
                         CHECK (processing_status IN ('pending','processed','failed','ignored')),
    attempts             smallint NOT NULL DEFAULT 0,
    last_error           text,
    CONSTRAINT webhook_events_unique UNIQUE (provider, external_event_id)
);
CREATE INDEX webhook_events_pending_idx ON webhook_events (processing_status, received_at);

-- -------------------------------------------------------------------------------------
-- 4. Tenant settings, document numbering and card fees
-- -------------------------------------------------------------------------------------

CREATE TABLE tenant_settings (
    tenant_id                  uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    deadline_alert_days        smallint NOT NULL DEFAULT 7,   -- prototype: prazoAlertaDias
    board_history_days         smallint NOT NULL DEFAULT 7,   -- prototype: osPrazoHistDias
    default_production_pct     numeric(7,4) NOT NULL DEFAULT 5,
    default_margin_pct         numeric(7,4) NOT NULL DEFAULT 35,
    default_discount_pct       numeric(7,4) NOT NULL DEFAULT 0,
    default_installments       smallint NOT NULL DEFAULT 1,
    default_down_payment_pct   numeric(7,4) NOT NULL DEFAULT 50,
    quote_validity_days        smallint NOT NULL DEFAULT 30,
    reserve_stock_on_quote     boolean NOT NULL DEFAULT true, -- BR-STK-01
    workday_start              time NOT NULL DEFAULT '08:00',
    workday_end                time NOT NULL DEFAULT '18:00',
    agenda_slot_minutes        smallint NOT NULL DEFAULT 15,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

-- Server-side document numbering (ADR-007). The DB stores only the integer;
-- the API renders "ORC-0025" from prefix + zero padding.
CREATE TABLE document_counters (
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_type text NOT NULL CHECK (document_type IN
                      ('quote','service_order','receipt','contract')),
    prefix        text NOT NULL,
    padding       smallint NOT NULL DEFAULT 4,
    last_number   bigint NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_type)
);
COMMENT ON TABLE document_counters IS
  'Allocate with: UPDATE document_counters SET last_number = last_number + 1 '
  'WHERE tenant_id = $1 AND document_type = $2 RETURNING last_number, prefix, padding. '
  'Single-row lock held until commit. Never use MAX(number)+1.';

CREATE TABLE card_fees (
    id            uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    installments  smallint NOT NULL CHECK (installments BETWEEN 1 AND 24),
    fee_pct       numeric(7,4) NOT NULL CHECK (fee_pct >= 0),
    is_active     boolean NOT NULL DEFAULT true,
    valid_from    date NOT NULL DEFAULT CURRENT_DATE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT card_fees_unique UNIQUE (tenant_id, installments, valid_from)
);
CREATE INDEX card_fees_lookup_idx ON card_fees (tenant_id, installments, valid_from DESC);

-- Contract / receipt wording per document type. Tenant configuration, which is why it
-- sits here rather than with the documents that use it: quotes reference it, and so do
-- attachments when the atelier uploads its own PDF instead of typing one.
CREATE TABLE contract_templates (
    id             uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_type  text NOT NULL CHECK (document_type IN ('sob_medida','ajuste_conserto','venda','aluguel')),
    title          text NOT NULL,
    source_type    text NOT NULL DEFAULT 'texto' CHECK (source_type IN ('texto','arquivo')),
    body_text      text,            -- source_type = 'texto'
    name           text NOT NULL,   -- shown on the quote: "Contrato padrão — sob medida"
    is_ready       boolean NOT NULL DEFAULT false,  -- user-editable: the atelier decides when
                                                   -- its wording is ready to print
    version        integer NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contract_templates_unique UNIQUE (tenant_id, document_type, version),
    -- Typed or attached, never both, never neither. An attached PDF lives in the
    -- `attachments` table with contract_template_id set.
    CONSTRAINT contract_templates_source_coherent CHECK (
        (source_type = 'texto'   AND body_text IS NOT NULL)
     OR (source_type = 'arquivo' AND body_text IS NULL)
    )
);

-- -------------------------------------------------------------------------------------
-- 5. Clients and the measurement sheet (Ficha de Medidas)
-- -------------------------------------------------------------------------------------

CREATE TABLE clients (
    id                uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    full_name         text NOT NULL,
    phone             text,
    email             citext,
    birth_date        date,
    profile_tag       text,          -- Noiva, Festa, Recorrente, Ajustes, Prospecção, Novo
    city              text,
    state_code        text,
    postal_code       text,
    address_line      text,
    cpf_ciphertext    bytea,         -- LGPD: column-level encryption (ADR-013)
    cpf_last3         text,          -- masked display "***.***.123-**"
    cpf_fingerprint   bytea,         -- HMAC-SHA256(cpf, tenant pepper): duplicate detection
    rg_ciphertext     bytea,
    notes             text,
    consent_marketing boolean NOT NULL DEFAULT false,
    consent_given_at  timestamptz,
    anonymized_at     timestamptz,   -- LGPD erasure without breaking financial history
    created_by        uuid REFERENCES users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz,
    CONSTRAINT clients_cpf_unique UNIQUE (tenant_id, cpf_fingerprint)
);
CREATE INDEX clients_tenant_name_idx ON clients (tenant_id, full_name) WHERE deleted_at IS NULL;
CREATE INDEX clients_birthmonth_idx
    ON clients (tenant_id, (EXTRACT(MONTH FROM birth_date)))
    WHERE deleted_at IS NULL AND birth_date IS NOT NULL;
CREATE INDEX clients_search_idx ON clients USING gin (to_tsvector('portuguese', full_name));

-- Catalogue of the 42 measurement fields. Drives backend validation and the absolute
-- positioning of the inputs over the croqui in the frontend. See ADR-016.
CREATE TABLE measurement_fields (
    key            text PRIMARY KEY,        -- ombroOmbroF, lBiceps, curvaturaLombar, ...
    panel          text NOT NULL CHECK (panel IN ('front_widths','front_heights','back')),
    column_side    text NOT NULL CHECK (column_side IN ('left','right')),
    sort_order     smallint NOT NULL,
    label_pt_br    text NOT NULL,
    value_type     text NOT NULL DEFAULT 'decimal' CHECK (value_type IN ('decimal','text')),
    unit           text,                    -- 'cm', or NULL for qualitative fields
    placeholder    text,
    top_pct        numeric(6,3) NOT NULL,   -- vertical anchor inside the 940x660 design box
    is_active      boolean NOT NULL DEFAULT true
);
COMMENT ON TABLE measurement_fields IS
  'Reference data, tenant-agnostic. Adding a field is a data migration, not a schema change. '
  'top_pct is the vertical CENTRE of the field row; calibrated 2026-09-24 (BT-01).';

-- One row per sheet version. Never updated in place: a new fitting creates a new version,
-- so the atelier keeps each client body history.
CREATE TABLE client_measurements (
    id             uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id      uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    version        integer NOT NULL,
    taken_on       date NOT NULL DEFAULT CURRENT_DATE,
    values         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"lBusto": 46.5, "curvaturaLombar": "M"}
    notes          text,
    is_current     boolean NOT NULL DEFAULT true,
    created_by     uuid REFERENCES users(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT client_measurements_version_unique UNIQUE (client_id, version),
    CONSTRAINT client_measurements_values_is_object CHECK (jsonb_typeof(values) = 'object')
);
CREATE UNIQUE INDEX client_measurements_current_idx
    ON client_measurements (client_id) WHERE is_current;
CREATE INDEX client_measurements_tenant_idx
    ON client_measurements (tenant_id, client_id, taken_on DESC);

-- -------------------------------------------------------------------------------------
-- 6. Catalogue: services, suppliers, materials, ready-to-wear
-- -------------------------------------------------------------------------------------

CREATE TABLE service_types (
    id             uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name           text NOT NULL,                       -- "Sob medida — Noiva", "Barra italiana"
    category       text NOT NULL CHECK (category IN ('sob_medida','ajuste_conserto')),
    pricing_mode   text NOT NULL CHECK (pricing_mode IN ('hourly','fixed_range','fixed')),
    unit           text NOT NULL DEFAULT 'Horas',       -- pt-BR unit shown in the UI
    hourly_cost    numeric(14,2),                       -- pricing_mode = hourly
    price_min      numeric(14,2),                       -- pricing_mode = fixed_range
    price_max      numeric(14,2),
    fixed_price    numeric(14,2),                       -- pricing_mode = fixed
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz,
    CONSTRAINT service_types_name_unique UNIQUE (tenant_id, name),
    CONSTRAINT service_types_pricing_coherent CHECK (
        (pricing_mode = 'hourly'      AND hourly_cost IS NOT NULL)
     OR (pricing_mode = 'fixed_range' AND price_min IS NOT NULL AND price_max IS NOT NULL
                                       AND price_max >= price_min)
     OR (pricing_mode = 'fixed'       AND fixed_price IS NOT NULL)
    )
);
CREATE INDEX service_types_tenant_idx ON service_types (tenant_id, category) WHERE deleted_at IS NULL;

CREATE TABLE suppliers (
    id             uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    number         bigint NOT NULL,             -- rendered "FOR-01" by the API
    name           text NOT NULL,
    category       text,                        -- Tecidos, Forros, Entretelas, Aviamentos...
    contact_name   text,
    phone          text,
    email          citext,
    address_line   text,
    city           text,
    notes          text,
    lead_time_days smallint,
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz,
    CONSTRAINT suppliers_number_unique UNIQUE (tenant_id, number),
    CONSTRAINT suppliers_name_unique   UNIQUE (tenant_id, name)
);
CREATE INDEX suppliers_tenant_idx ON suppliers (tenant_id) WHERE deleted_at IS NULL;

-- "Estoque e Insumos". quantity_on_hand is a denormalised projection of stock_movements,
-- maintained by trigger so the board and quote editor never SUM over the ledger.
CREATE TABLE materials (
    id                  uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                text NOT NULL,
    sku                 text,                         -- "MUS-01"
    color               text,
    unit                text NOT NULL DEFAULT 'm',    -- m, pç, un (pt-BR abbreviations)
    quantity_on_hand    numeric(14,3) NOT NULL DEFAULT 0,
    minimum_quantity    numeric(14,3) NOT NULL DEFAULT 0,
    current_unit_cost   numeric(14,2) NOT NULL DEFAULT 0,
    default_supplier_id uuid REFERENCES suppliers(id) ON DELETE SET NULL,
    storage_location    text,                         -- "Prat. A2"
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    CONSTRAINT materials_name_unique UNIQUE (tenant_id, name),
    CONSTRAINT materials_sku_unique  UNIQUE (tenant_id, sku)
);
CREATE INDEX materials_tenant_idx ON materials (tenant_id) WHERE deleted_at IS NULL;
CREATE INDEX materials_low_stock_idx ON materials (tenant_id)
    WHERE deleted_at IS NULL AND quantity_on_hand <= minimum_quantity;

CREATE TABLE material_cost_history (
    id            uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    material_id   uuid NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    changed_on    date NOT NULL DEFAULT CURRENT_DATE,
    cost_from     numeric(14,2),
    cost_to       numeric(14,2) NOT NULL,
    source        text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','stock_entry','import')),
    supplier_id   uuid REFERENCES suppliers(id) ON DELETE SET NULL,
    created_by    uuid REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX material_cost_history_idx ON material_cost_history (tenant_id, material_id, changed_on DESC);

-- Append-only stock ledger. quantity is SIGNED: positive = in, negative = out.
CREATE TABLE stock_movements (
    id              uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    material_id     uuid NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    movement_type   text NOT NULL CHECK (movement_type IN
                        ('entrada','consumo','devolucao','ajuste','perda','inventario')),
    quantity        numeric(14,3) NOT NULL CHECK (quantity <> 0),
    unit_cost       numeric(14,2),
    total_cost      numeric(14,2) GENERATED ALWAYS AS (abs(quantity) * COALESCE(unit_cost, 0)) STORED,
    reference_type  text CHECK (reference_type IN ('quote','service_order','supplier_invoice','manual')),
    reference_id    uuid,
    supplier_id     uuid REFERENCES suppliers(id) ON DELETE SET NULL,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    note            text,
    created_by      uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX stock_movements_material_idx  ON stock_movements (tenant_id, material_id, occurred_at DESC);
CREATE INDEX stock_movements_reference_idx ON stock_movements (tenant_id, reference_type, reference_id);

-- "Pronta Entrega" — finished pieces for sale or rental.
--
-- A CATALOGUE ONLY. Selling or renting a piece is an ordinary quote of type 'venda' or
-- 'aluguel' with a quote_items line of kind 'pronta_entrega'; approval creates a service
-- order exactly as for made-to-measure work, and delivery feeds the cash flow through the
-- normal path. There is deliberately no separate ready-to-wear transaction pipeline.
--
-- Occupancy (committed on a live quote, or out on an active rental) is therefore DERIVED
-- from quote_items joined to service_orders — see v_rtw_availability — and never stored.
CREATE TABLE ready_to_wear_items (
    id              uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            text NOT NULL,
    category        text,                              -- Vestido, Véu, Acessório
    description     text,                              -- "Tam. 38 · bordado em pedraria"
    size_label      text,
    color           text,
    sale_price      numeric(14,2),
    rental_price    numeric(14,2),
    units_total     integer NOT NULL DEFAULT 0 CHECK (units_total >= 0),

    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX rtw_tenant_idx ON ready_to_wear_items (tenant_id) WHERE deleted_at IS NULL;

-- Ready-to-wear now gets the same two histories as Estoque e Insumos (owner decision,
-- 2026-09-26, closing OI-DB-07): an append-only unit ledger and a price history. Before
-- this, units_total was edited with no recorded reason — you could see *that* it changed
-- in audit_log, but not why, unlike a material balance.
CREATE TABLE rtw_movements (
    id             uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    item_id        uuid NOT NULL REFERENCES ready_to_wear_items(id) ON DELETE RESTRICT,
    movement_type  text NOT NULL CHECK (movement_type IN
                       ('entrada','venda','perda','avaria','ajuste','inventario')),
    quantity       integer NOT NULL CHECK (quantity <> 0),   -- signed: + in, - out
    unit_cost      numeric(14,2),
    reference_type text CHECK (reference_type IN ('service_order','manual')),
    reference_id   uuid,                                     -- the venda order, when applicable
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    note           text,
    created_by     uuid REFERENCES users(id),
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rtw_movements_item_idx ON rtw_movements (tenant_id, item_id, occurred_at DESC);

CREATE TABLE rtw_price_history (
    id          uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    item_id     uuid NOT NULL REFERENCES ready_to_wear_items(id) ON DELETE CASCADE,
    price_type  text NOT NULL CHECK (price_type IN ('venda','aluguel')),
    changed_on  date NOT NULL DEFAULT CURRENT_DATE,
    price_from  numeric(14,2),
    price_to    numeric(14,2) NOT NULL,
    created_by  uuid REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rtw_price_history_idx ON rtw_price_history (tenant_id, item_id, changed_on DESC);

-- Same shape as materials: the cached count is a projection of the ledger.
CREATE OR REPLACE FUNCTION cineatelie.apply_rtw_movement()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    UPDATE ready_to_wear_items
       SET units_total = units_total + NEW.quantity, updated_at = now()
     WHERE id = NEW.item_id AND tenant_id = NEW.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ready-to-wear item % not found for tenant %', NEW.item_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$fn$;
CREATE TRIGGER rtw_movements_apply
    AFTER INSERT ON rtw_movements
    FOR EACH ROW EXECUTE FUNCTION cineatelie.apply_rtw_movement();
CREATE TRIGGER rtw_movements_immutable
    BEFORE UPDATE OR DELETE ON rtw_movements
    FOR EACH ROW EXECUTE FUNCTION cineatelie.forbid_write();
COMMENT ON COLUMN ready_to_wear_items.units_total IS
  'Units owned. Delivering a VENDA order decrements this (the piece leaves permanently); '
  'delivering an ALUGUEL order does not. Deliberately simpler than the materials ledger: '
  'RTW holdings are a handful of pieces, so a movement ledger would be over-engineering.';


-- -------------------------------------------------------------------------------------
-- 7. Commercial: quotes
-- -------------------------------------------------------------------------------------

CREATE TABLE quotes (
    id                    uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    number                bigint NOT NULL,                       -- rendered "ORC-0025"
    client_id             uuid NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    title                 text NOT NULL,                         -- peça: "Vestido musseline"
    description           text,
    quote_type            text NOT NULL CHECK (quote_type IN ('sob_medida','ajuste_conserto','venda','aluguel')),
    status                text NOT NULL DEFAULT 'rascunho'
                          CHECK (status IN ('rascunho','enviado','aprovado','recusado','expirado','cancelado')),
    issued_on             date NOT NULL DEFAULT CURRENT_DATE,
    valid_until           date,
    delivery_due_on       date,
    -- Rental only (quote_type = 'aluguel'): when the piece is due back. Carried onto the
    -- service order at approval, where it stays editable, and it is what schedules the
    -- Agenda reminder when the order is delivered (BR-RTW-04).
    rental_return_due_on  date,
    -- Which contract this quote prints with (BR-CTR-03). Shown by name on the quote and
    -- appended to its PDF. NULL = no contract attached to this quote.
    contract_template_id  uuid REFERENCES contract_templates(id) ON DELETE SET NULL,
    -- pricing inputs (snapshot at quote time)
    production_pct        numeric(7,4) NOT NULL DEFAULT 0 CHECK (production_pct >= 0),
    margin_pct            numeric(7,4) NOT NULL DEFAULT 0 CHECK (margin_pct >= 0),
    discount_pct          numeric(7,4) NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
    payment_method        text CHECK (payment_method IN ('dinheiro','pix','cartao')),
    installments          smallint NOT NULL DEFAULT 1 CHECK (installments BETWEEN 1 AND 24),
    down_payment_pct      numeric(7,4) CHECK (down_payment_pct BETWEEN 0 AND 100),
    card_fee_pct          numeric(7,4) NOT NULL DEFAULT 0,       -- snapshot from card_fees
    -- pricing outputs (computed by the backend, persisted for reporting and immutability)
    subtotal_amount       numeric(14,2) NOT NULL DEFAULT 0,
    production_amount     numeric(14,2) NOT NULL DEFAULT 0,
    margin_amount         numeric(14,2) NOT NULL DEFAULT 0,
    discount_amount       numeric(14,2) NOT NULL DEFAULT 0,
    cash_amount           numeric(14,2) NOT NULL DEFAULT 0,      -- preço à vista
    card_amount           numeric(14,2) NOT NULL DEFAULT 0,      -- preço no cartão
    profit_amount         numeric(14,2) NOT NULL DEFAULT 0,
    effective_margin_pct  numeric(7,4) NOT NULL DEFAULT 0,
    notes                 text,
    sent_at               timestamptz,
    approved_at           timestamptz,
    refused_at            timestamptz,
    refusal_reason        text,
    created_by            uuid REFERENCES users(id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    deleted_at            timestamptz,
    CONSTRAINT quotes_number_unique UNIQUE (tenant_id, number)
);
CREATE INDEX quotes_tenant_status_idx ON quotes (tenant_id, status, issued_on DESC) WHERE deleted_at IS NULL;
CREATE INDEX quotes_client_idx        ON quotes (tenant_id, client_id)              WHERE deleted_at IS NULL;

CREATE TABLE quote_items (
    id               uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    quote_id         uuid NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    line_no          smallint NOT NULL,
    item_kind        text NOT NULL CHECK (item_kind IN ('material','servico','pronta_entrega','avulso')),
    material_id      uuid REFERENCES materials(id) ON DELETE RESTRICT,
    service_type_id  uuid REFERENCES service_types(id) ON DELETE RESTRICT,
    ready_to_wear_item_id uuid REFERENCES ready_to_wear_items(id) ON DELETE RESTRICT,
    description      text NOT NULL,                  -- snapshot of the catalogue name
    quantity         numeric(14,3) NOT NULL CHECK (quantity > 0),
    unit             text NOT NULL,
    unit_price       numeric(14,2) NOT NULL CHECK (unit_price >= 0),
    line_total       numeric(14,2) GENERATED ALWAYS AS (round(quantity * unit_price, 2)) STORED,
    from_stock       boolean NOT NULL DEFAULT false, -- drives stock reservation
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT quote_items_line_unique UNIQUE (quote_id, line_no),
    CONSTRAINT quote_items_kind_coherent CHECK (
        (item_kind = 'material'       AND material_id IS NOT NULL AND service_type_id IS NULL AND ready_to_wear_item_id IS NULL)
     OR (item_kind = 'servico'        AND service_type_id IS NOT NULL AND material_id IS NULL AND ready_to_wear_item_id IS NULL)
     OR (item_kind = 'pronta_entrega' AND ready_to_wear_item_id IS NOT NULL AND material_id IS NULL AND service_type_id IS NULL)
     OR (item_kind = 'avulso'         AND material_id IS NULL AND service_type_id IS NULL AND ready_to_wear_item_id IS NULL)
    ),
    CONSTRAINT quote_items_stock_needs_material CHECK (NOT from_stock OR material_id IS NOT NULL)
);
CREATE INDEX quote_items_quote_idx ON quote_items (tenant_id, quote_id, line_no);
CREATE INDEX quote_items_rtw_idx   ON quote_items (tenant_id, ready_to_wear_item_id)
    WHERE ready_to_wear_item_id IS NOT NULL;

-- Soft allocation created when a quote line consumes a stock material (BR-STK-01).
-- Released when the quote is refused/cancelled; consumed when the order enters production.
--
-- Declared here, AFTER quote_items, so that quote_id and quote_item_id can be real foreign
-- keys. An earlier draft placed this with the stock tables and left them as bare uuids,
-- which was an unnecessary integrity gap: nothing in quotes references stock_reservations,
-- so there was never a genuine circular dependency — only file ordering (closed OI-DB-05).
CREATE TABLE stock_reservations (
    id              uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    material_id     uuid NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    quote_id        uuid NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    quote_item_id   uuid NOT NULL REFERENCES quote_items(id) ON DELETE CASCADE,
    quantity        numeric(14,3) NOT NULL CHECK (quantity > 0),
    status          text NOT NULL DEFAULT 'reserved'
                    CHECK (status IN ('reserved','released','consumed')),
    reserved_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolution_note text,
    CONSTRAINT stock_reservations_item_unique UNIQUE (quote_item_id)
);
CREATE INDEX stock_reservations_open_idx
    ON stock_reservations (tenant_id, material_id) WHERE status = 'reserved';
CREATE INDEX stock_reservations_quote_idx ON stock_reservations (tenant_id, quote_id);

-- Server-side autosave of the quote editor, so nothing is lost if the tab closes.
CREATE TABLE quote_drafts (
    id           uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    quote_id     uuid REFERENCES quotes(id) ON DELETE CASCADE,  -- NULL = brand-new quote
    payload      jsonb NOT NULL,
    client_rev   bigint NOT NULL DEFAULT 1,                     -- optimistic concurrency
    updated_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL DEFAULT now() + interval '30 days',
    CONSTRAINT quote_drafts_scope_unique UNIQUE (tenant_id, user_id, quote_id)
);
CREATE INDEX quote_drafts_expiry_idx ON quote_drafts (expires_at);

-- -------------------------------------------------------------------------------------
-- 8. Production: service orders (Kanban)
-- -------------------------------------------------------------------------------------

CREATE TABLE service_orders (
    id                   uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    number               bigint NOT NULL,                       -- rendered "OS-0035"
    quote_id             uuid REFERENCES quotes(id) ON DELETE SET NULL,
    client_id            uuid NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    title                text NOT NULL,                         -- peça
    order_type           text NOT NULL CHECK (order_type IN ('sob_medida','ajuste_conserto','venda','aluguel')),
    stage                text NOT NULL DEFAULT 'aprovado'
                         CHECK (stage IN ('aprovado','em_producao','prova','concluido','entregue','cancelado')),
    stage_changed_at     timestamptz NOT NULL DEFAULT now(),
    board_position       integer NOT NULL DEFAULT 0,            -- order within the column
    delivery_due_on      date,
    -- Rental only (order_type = 'aluguel'). Copied from the quote at approval and editable
    -- here. Reaching stage 'entregue' schedules an Agenda reminder on this date; the piece
    -- counts as unavailable until rental_returned_at is set (BR-RTW-04, BR-RTW-05).
    rental_return_due_on date,
    rental_returned_at   timestamptz,
    total_amount         numeric(14,2) NOT NULL DEFAULT 0,
    payment_method       text CHECK (payment_method IN ('dinheiro','pix','cartao')),
    installments         smallint NOT NULL DEFAULT 1,
    is_settled           boolean NOT NULL DEFAULT false,        -- "Quitado"
    settled_at           timestamptz,
    delivered_at         timestamptz,
    cancelled_at         timestamptz,
    cancellation_reason  text,
    closed_at            timestamptz,                           -- manually removed from the board
    notes                text,
    created_by           uuid REFERENCES users(id),
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz,
    CONSTRAINT service_orders_number_unique UNIQUE (tenant_id, number),
    CONSTRAINT service_orders_quote_unique  UNIQUE (tenant_id, quote_id),
    CONSTRAINT service_orders_cancel_reason CHECK (stage <> 'cancelado' OR cancellation_reason IS NOT NULL),
    -- Rental-only fields must stay empty on any other order type.
    CONSTRAINT service_orders_rental_fields CHECK (
        order_type = 'aluguel' OR (rental_return_due_on IS NULL AND rental_returned_at IS NULL)
    )
);
CREATE INDEX service_orders_board_idx
    ON service_orders (tenant_id, stage, board_position)
    WHERE deleted_at IS NULL AND closed_at IS NULL;
CREATE INDEX service_orders_due_idx ON service_orders (tenant_id, delivery_due_on) WHERE deleted_at IS NULL;
-- Rentals still out: the dashboard's overdue-return feed.
CREATE INDEX service_orders_rental_out_idx ON service_orders (tenant_id, rental_return_due_on)
    WHERE order_type = 'aluguel' AND stage = 'entregue' AND rental_returned_at IS NULL AND deleted_at IS NULL;
CREATE INDEX service_orders_history_idx
    ON service_orders (tenant_id, delivered_at DESC) WHERE stage IN ('entregue','cancelado');

CREATE TABLE service_order_stage_history (
    id                 uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_order_id   uuid NOT NULL REFERENCES service_orders(id) ON DELETE CASCADE,
    from_stage         text,
    to_stage           text NOT NULL,
    reason             text,
    changed_by         uuid REFERENCES users(id),
    changed_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX so_stage_history_idx
    ON service_order_stage_history (tenant_id, service_order_id, changed_at DESC);

-- Attachment METADATA. The bytes live in a private object-storage bucket and never touch
-- the database or the API container (ADR-018) — a deliberate choice reinforced by the free
-- tier's shape: 1 GB of file storage against 500 MB of database, where database space also
-- has to hold every business row, index and WAL segment.
--
-- ONE table for all three owners, rather than one per owner: service orders take receipts and
-- fitting photos, ready-to-wear pieces take product photos, contract templates take the
-- atelier's own PDF contract, and quota enforcement needs a
-- single place to count and sum (BR-ATT-01). Exactly one owner column is non-null.
CREATE TABLE attachments (
    id                    uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_order_id      uuid REFERENCES service_orders(id) ON DELETE CASCADE,
    ready_to_wear_item_id uuid REFERENCES ready_to_wear_items(id) ON DELETE CASCADE,
    contract_template_id  uuid REFERENCES contract_templates(id) ON DELETE CASCADE,
    label                 text NOT NULL,            -- "Comprovante do sinal"
    storage_path          text NOT NULL,            -- tenants/<tenant_id>/orders/<id>/<uuid>.jpg
    thumbnail_path        text,                     -- 70x70 derivative; what grids and cards
                                                    -- render, to keep egress down (ADR-018)
    mime_type             text NOT NULL,
    size_bytes            bigint NOT NULL CHECK (size_bytes > 0),
    checksum_sha256       bytea,
    sort_order            smallint NOT NULL DEFAULT 0,
    uploaded_by           uuid REFERENCES users(id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    deleted_at            timestamptz,
    CONSTRAINT attachments_path_unique UNIQUE (storage_path),
    -- Exactly one owner. A contract-template attachment is the atelier's own PDF contract
    -- (BR-CTR-02): it counts against storage but NOT against attachments_per_item, which is
    -- a per-order / per-piece limit.
    CONSTRAINT attachments_one_owner CHECK (
        (service_order_id IS NOT NULL)::int
      + (ready_to_wear_item_id IS NOT NULL)::int
      + (contract_template_id IS NOT NULL)::int = 1
    )
);
CREATE INDEX attachments_order_idx
    ON attachments (tenant_id, service_order_id, sort_order)
    WHERE deleted_at IS NULL AND service_order_id IS NOT NULL;
CREATE INDEX attachments_rtw_idx
    ON attachments (tenant_id, ready_to_wear_item_id, sort_order)
    WHERE deleted_at IS NULL AND ready_to_wear_item_id IS NOT NULL;
CREATE INDEX attachments_contract_idx
    ON attachments (tenant_id, contract_template_id)
    WHERE deleted_at IS NULL AND contract_template_id IS NOT NULL;
CREATE INDEX attachments_tenant_size_idx
    ON attachments (tenant_id) WHERE deleted_at IS NULL;

-- -------------------------------------------------------------------------------------
-- 9. Agenda
-- -------------------------------------------------------------------------------------

CREATE TABLE appointments (
    id                uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title             text NOT NULL,
    notes             text,
    kind              text NOT NULL DEFAULT 'compromisso'
                      CHECK (kind IN ('compromisso','prova','entrega','devolucao','reuniao')),
    starts_at         timestamptz NOT NULL,
    ends_at           timestamptz NOT NULL,
    is_all_day        boolean NOT NULL DEFAULT false,
    client_id         uuid REFERENCES clients(id) ON DELETE SET NULL,
    service_order_id  uuid REFERENCES service_orders(id) ON DELETE CASCADE,
    assigned_to       uuid REFERENCES users(id) ON DELETE SET NULL,
    status            text NOT NULL DEFAULT 'agendado'
                      CHECK (status IN ('agendado','concluido','cancelado')),
    created_by        uuid REFERENCES users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz,
    CONSTRAINT appointments_time_valid CHECK (ends_at > starts_at)
);
CREATE INDEX appointments_tenant_range_idx ON appointments (tenant_id, starts_at) WHERE deleted_at IS NULL;
CREATE INDEX appointments_order_idx
    ON appointments (tenant_id, service_order_id) WHERE service_order_id IS NOT NULL;

-- -------------------------------------------------------------------------------------
-- 10. Finance (Fluxo de Caixa)
-- -------------------------------------------------------------------------------------

CREATE TABLE finance_categories (
    id           uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         text NOT NULL,                 -- "Compra de material", "Taxas de cartão"
    direction    text NOT NULL CHECK (direction IN ('receita','despesa','ambos')),
    is_system    boolean NOT NULL DEFAULT false,
    is_active    boolean NOT NULL DEFAULT true,
    CONSTRAINT finance_categories_unique UNIQUE (tenant_id, name)
);

-- One row per instalment. amount is always POSITIVE; `direction` gives the sign.
-- "Em atraso" is DERIVED, never stored: an unpaid entry whose due_date precedes the
-- first day of the month being viewed. See ADR-012 and fn_cash_flow_month below.
CREATE TABLE finance_entries (
    id                  uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    direction           text NOT NULL CHECK (direction IN ('receita','despesa')),
    description         text NOT NULL,
    category_id         uuid REFERENCES finance_categories(id) ON DELETE SET NULL,
    amount              numeric(14,2) NOT NULL CHECK (amount > 0),
    due_date            date NOT NULL,
    paid_on             date,
    status              text NOT NULL DEFAULT 'previsto'
                        CHECK (status IN ('previsto','confirmado','liquidado','cancelado')),
    payment_method      text,                        -- free text: "Pix", "Cartão 3x"
    client_id           uuid REFERENCES clients(id) ON DELETE SET NULL,
    supplier_id         uuid REFERENCES suppliers(id) ON DELETE SET NULL,
    source              text NOT NULL DEFAULT 'manual'
                        CHECK (source IN ('manual','ordem_servico','assinatura','importacao')),
    service_order_id    uuid REFERENCES service_orders(id) ON DELETE SET NULL,
    installment_no      smallint NOT NULL DEFAULT 1,
    installment_count   smallint NOT NULL DEFAULT 1,
    -- Set when a DELIVERED order is cancelled and this entry was already settled: the money
    -- was really received, and only the atelier can decide whether it is kept, refunded or
    -- disputed (BR-ORD-12). Surfaces in the "Para revisão" section of Fluxo de Caixa.
    needs_review        boolean NOT NULL DEFAULT false,
    review_reason       text,
    notes               text,
    created_by          uuid REFERENCES users(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    CONSTRAINT finance_entries_paid_coherent
        CHECK (status <> 'liquidado' OR paid_on IS NOT NULL),
    CONSTRAINT finance_entries_installment_valid
        CHECK (installment_no BETWEEN 1 AND installment_count)
);
CREATE INDEX finance_entries_month_idx ON finance_entries (tenant_id, due_date) WHERE deleted_at IS NULL;
CREATE INDEX finance_entries_open_idx  ON finance_entries (tenant_id, due_date)
    WHERE deleted_at IS NULL AND status IN ('previsto','confirmado');
CREATE INDEX finance_entries_review_idx ON finance_entries (tenant_id) WHERE needs_review;
CREATE INDEX finance_entries_order_idx ON finance_entries (tenant_id, service_order_id)
    WHERE service_order_id IS NOT NULL;

-- -------------------------------------------------------------------------------------
-- 11. Documents: adjustment receipts (contract_templates lives in section 4)
-- -------------------------------------------------------------------------------------

CREATE TABLE receipts (
    id                uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    number            bigint NOT NULL,                  -- rendered "REC-0142"
    client_id         uuid NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    service_order_id  uuid REFERENCES service_orders(id) ON DELETE SET NULL,
    quote_id          uuid REFERENCES quotes(id) ON DELETE SET NULL,
    issued_on         date NOT NULL DEFAULT CURRENT_DATE,
    status            text NOT NULL DEFAULT 'em_aberto'
                      CHECK (status IN ('em_aberto','pago','cancelado')),
    installments      smallint NOT NULL DEFAULT 1,
    total_amount      numeric(14,2) NOT NULL DEFAULT 0,
    contract_snapshot text,                             -- template body frozen at issue time
    created_by        uuid REFERENCES users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz,
    CONSTRAINT receipts_number_unique UNIQUE (tenant_id, number)
);
CREATE INDEX receipts_tenant_idx ON receipts (tenant_id, issued_on DESC) WHERE deleted_at IS NULL;

CREATE TABLE receipt_items (
    id           uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    receipt_id   uuid NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    line_no      smallint NOT NULL,
    description  text NOT NULL,
    quantity     numeric(14,3) NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price   numeric(14,2) NOT NULL CHECK (unit_price >= 0),
    line_total   numeric(14,2) GENERATED ALWAYS AS (round(quantity * unit_price, 2)) STORED,
    CONSTRAINT receipt_items_line_unique UNIQUE (receipt_id, line_no)
);

-- -------------------------------------------------------------------------------------
-- 12. Audit, LGPD and the async job queue
-- -------------------------------------------------------------------------------------

-- PARTITIONED BY MONTH. This is the only table that combines a high insert rate, a long
-- retention window and periodic bulk deletion, which is precisely the partitioning case.
-- Retention is then DROP TABLE on a whole partition: instant, no dead tuples, no WAL for
-- row deletes, and no heap or index bloat for autovacuum to chase. See ADR-019 and the
-- database spec §5.
--
-- Note the composite primary key: PostgreSQL requires the partition key in every unique
-- constraint. FKs do not reference audit_log, so nothing else is affected.
--
-- No foreign keys on tenant_id / actor_user_id either: an audit row must survive the
-- deletion of the tenant or user it describes, which is the whole point of an audit trail.
CREATE TABLE audit_log (
    id             uuid NOT NULL DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid,
    actor_user_id  uuid,
    actor_role     text,
    action         text NOT NULL,                -- quote.approved, order.stage_changed
    entity_type    text NOT NULL,
    entity_id      uuid,
    before_state   jsonb,                        -- see the rule below on personal data
    after_state    jsonb,
    request_id     text,
    ip_address     inet,
    user_agent     text,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

COMMENT ON TABLE audit_log IS
  'Append-only business audit trail, partitioned by month. NEVER store personal-data VALUES '
  'in before_state/after_state — record the field NAMES that changed instead. That keeps the '
  'trail free of CPF, RG and body measurements, so an LGPD erasure never has to rewrite it '
  'and partition drop remains the only deletion path.';

-- Indexes declared on the parent propagate to every partition, existing and future.
CREATE INDEX audit_log_tenant_idx ON audit_log (tenant_id, occurred_at DESC);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id, occurred_at DESC);

-- Safety net: catches rows whose month has no partition yet, so an insert can never fail
-- because the maintenance job lagged. It should stay empty; the job creates partitions ahead.
CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT;

-- A partitioned table's RLS policy is NOT automatically enforced on its partitions when a
-- partition is queried directly by name (only when queried through the parent) — confirmed
-- the hard way, by the isolation suite: `app_user`, no tenant context, querying
-- `audit_log_default` directly returned real rows while the same query through `audit_log`
-- correctly returned zero. ENABLE + FORCE here is what makes the parent's own policy apply
-- to the partition too; no second policy needs creating, the existing one is inherited once
-- this is on. Every future monthly partition needs the same two lines — see
-- ensure_audit_log_partitions() below, where they are not optional either.
ALTER TABLE audit_log_default ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log_default FORCE  ROW LEVEL SECURITY;

-- Create the monthly partitions covering [now - p_months_back, now + p_months_ahead].
-- Idempotent: run it as often as you like. Called by the housekeeping job.
-- SECURITY DEFINER: creating and dropping partitions requires CREATE on the schema and
-- ownership of the partition, neither of which app_worker has (nor should have). These two
-- functions are the only sanctioned path, and both are granted to app_worker alone.
CREATE OR REPLACE FUNCTION cineatelie.ensure_audit_log_partitions(
    p_months_ahead int DEFAULT 3,
    p_months_back  int DEFAULT 1
)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = cineatelie, extensions
AS $fn$
DECLARE
    m           date;
    part_name   text;
    created     int := 0;
BEGIN
    FOR m IN
        SELECT generate_series(
                   date_trunc('month', CURRENT_DATE - (p_months_back  || ' months')::interval),
                   date_trunc('month', CURRENT_DATE + (p_months_ahead || ' months')::interval),
                   interval '1 month'
               )::date
    LOOP
        part_name := format('audit_log_%s', to_char(m, 'YYYY_MM'));
        -- Schema-qualified: 'public.' here would make this check always miss (the schema
        -- is cineatelie, not public) and re-attempt CREATE TABLE on a partition that
        -- already exists, failing with "relation already exists" — a stale reference from
        -- before the schema rename, caught only by re-running this function twice, which
        -- the isolation suite's setup effectively did.
        IF to_regclass('cineatelie.' || part_name) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
                part_name, m, (m + interval '1 month')::date);
            -- See the comment on audit_log_default above: this does not happen
            -- automatically for a partition, only for the parent.
            EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', part_name);
            EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', part_name);
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END;
$fn$;

-- Retention by partition drop. Drops every whole monthly partition that ends on or before
-- the cutoff. Never touches audit_log_default, and never partially deletes a partition.
CREATE OR REPLACE FUNCTION cineatelie.drop_audit_log_partitions_before(p_cutoff date)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = cineatelie, extensions
AS $fn$
DECLARE
    r        record;
    dropped  int := 0;
BEGIN
    FOR r IN
        SELECT c.relname,
               (regexp_match(c.relname, '^audit_log_(\d{4})_(\d{2})$')) AS parts
          FROM pg_class c
          JOIN pg_inherits i ON i.inhrelid = c.oid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE p.relname = 'audit_log'
           AND c.relname ~ '^audit_log_\d{4}_\d{2}$'
    LOOP
        IF (make_date(r.parts[1]::int, r.parts[2]::int, 1) + interval '1 month')::date
           <= p_cutoff THEN
            EXECUTE format('DROP TABLE %I', r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$fn$;

-- Seed the initial partitions so the schema is usable immediately.
SELECT cineatelie.ensure_audit_log_partitions(3, 1);

-- Only the worker may manage partitions, and only through these two functions.
REVOKE EXECUTE ON FUNCTION cineatelie.ensure_audit_log_partitions(int, int) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION cineatelie.drop_audit_log_partitions_before(date) FROM PUBLIC;

CREATE TABLE lgpd_requests (
    id             uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id      uuid REFERENCES tenants(id) ON DELETE CASCADE,
    subject_type   text NOT NULL CHECK (subject_type IN ('client','user')),
    subject_id     uuid NOT NULL,
    request_type   text NOT NULL
                   CHECK (request_type IN ('access','portability','rectification','erasure','anonymization')),
    status         text NOT NULL DEFAULT 'received'
                   CHECK (status IN ('received','in_progress','completed','rejected')),
    requested_at   timestamptz NOT NULL DEFAULT now(),
    due_at         timestamptz,
    completed_at   timestamptz,
    handled_by     uuid REFERENCES users(id),
    export_path    text,
    notes          text
);
CREATE INDEX lgpd_requests_open_idx ON lgpd_requests (status, requested_at);

-- PostgreSQL-backed job queue for heavy/batch work, so the zero-budget deployment
-- needs no Redis. See ADR-010.
-- Replay cache for the Idempotency-Key header (ADR-014). Without this, "a replayed key
-- In-app notification centre — the bell icon and its unread count (BR-NOT-01).
--
-- Everything in the product is currently PULL: deadlines, low stock and overdue rental
-- returns are visible only if you happen to open the right screen. This is the first push
-- surface, and it is deliberately in-app only: no e-mail provider, no browser permission
-- prompt, no external dependency, and it works on the free tier.
--
-- Rows are GENERATED, never written by a user. They are also disposable: a notification is
-- a pointer to a fact that lives elsewhere, so pruning one loses nothing.
CREATE TABLE notifications (
    id           uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- NULL = for everyone in the workspace. Set = for one person, e.g. an order assigned
    -- to a specific seamstress.
    user_id      uuid REFERENCES users(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN
                     ('prazo_proximo','prazo_vencido','devolucao_prevista','devolucao_atrasada',
                      'estoque_baixo','pagamento_vencido','orcamento_expirando','sistema')),
    severity     text NOT NULL DEFAULT 'info' CHECK (severity IN ('info','atencao','urgente')),
    title        text NOT NULL,          -- pt-BR, already rendered
    body         text,
    -- Where clicking it goes, e.g. {"screen":"ordens","service_order_id":"..."}
    link         jsonb,
    -- Dedup key so a daily sweep re-running does not stack identical rows
    dedup_key    text NOT NULL,
    read_at      timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT notifications_dedup_unique UNIQUE (tenant_id, user_id, dedup_key)
);
CREATE INDEX notifications_unread_idx
    ON notifications (tenant_id, user_id, created_at DESC) WHERE read_at IS NULL;
CREATE INDEX notifications_created_idx ON notifications (tenant_id, created_at DESC);

-- returns the original response" is unimplementable — the response has to be stored.
--
-- Short-lived by design: this is a deduplication window for retries, not a record. The
-- default is 24 hours (RETENTION_IDEMPOTENCY_HOURS), which comfortably covers a client
-- retry storm or a user double-clicking "Aprovar".
CREATE TABLE idempotency_records (
    id              uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         uuid REFERENCES users(id) ON DELETE SET NULL,
    idempotency_key text NOT NULL,
    route           text NOT NULL,          -- method + path template, e.g. "POST /quotes"
    request_hash    bytea NOT NULL,         -- sha256 of the body: detects key reuse with a
                                            -- DIFFERENT payload, which is a client bug (BR-API-01)
    status_code     smallint,
    response_body   jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,            -- NULL while the original request is still in flight
    expires_at      timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    CONSTRAINT idempotency_records_unique UNIQUE (tenant_id, idempotency_key, route)
);
CREATE INDEX idempotency_records_expiry_idx ON idempotency_records (expires_at);

CREATE TABLE job_queue (
    id              uuid PRIMARY KEY DEFAULT cineatelie.uuid_generate_v7(),
    tenant_id       uuid REFERENCES tenants(id) ON DELETE CASCADE,
    job_type        text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','succeeded','failed','dead')),
    priority        smallint NOT NULL DEFAULT 100,
    run_after       timestamptz NOT NULL DEFAULT now(),
    attempts        smallint NOT NULL DEFAULT 0,
    max_attempts    smallint NOT NULL DEFAULT 5,
    locked_at       timestamptz,
    locked_by       text,
    last_error      text,
    idempotency_key text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    CONSTRAINT job_queue_idempotency_unique UNIQUE (job_type, idempotency_key)
);
CREATE INDEX job_queue_pickup_idx ON job_queue (status, run_after, priority) WHERE status = 'queued';
COMMENT ON TABLE job_queue IS
  'Workers claim with SELECT ... FOR UPDATE SKIP LOCKED LIMIT n, so one tenant batch '
  'never blocks another tenant.';

-- -------------------------------------------------------------------------------------
-- 13. Triggers
-- -------------------------------------------------------------------------------------

-- updated_at on every mutable table
DO $do$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users','tenants','memberships','subscriptions','tenant_settings','card_fees',
        'clients','service_types','suppliers','materials','ready_to_wear_items',
        'quotes','service_orders','appointments','finance_entries','contract_templates','receipts'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$I_touch_updated_at BEFORE UPDATE ON %1$I
             FOR EACH ROW EXECUTE FUNCTION cineatelie.touch_updated_at()', t);
    END LOOP;
END
$do$;

-- tenant guard on every tenant-scoped table
DO $do$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'memberships','tenant_invitations','subscriptions','tenant_feature_overrides',
        'billing_invoices','tenant_quota_overrides','tenant_settings','document_counters',
        'card_fees','clients',
        'client_measurements','service_types','suppliers','materials','material_cost_history',
        'stock_movements','stock_reservations','ready_to_wear_items','quotes','quote_items',
        'quote_drafts','service_orders','service_order_stage_history','attachments',
        'appointments','finance_categories','finance_entries','contract_templates','receipts',
        'receipt_items','idempotency_records','notifications','rtw_movements','rtw_price_history'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$I_assert_tenant BEFORE INSERT OR UPDATE ON %1$I
             FOR EACH ROW EXECUTE FUNCTION cineatelie.assert_tenant_matches()', t);
    END LOOP;
END
$do$;

-- Keep materials.quantity_on_hand in sync with the append-only ledger.
CREATE OR REPLACE FUNCTION cineatelie.apply_stock_movement()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    UPDATE materials
       SET quantity_on_hand = quantity_on_hand + NEW.quantity,
           updated_at       = now()
     WHERE id = NEW.material_id
       AND tenant_id = NEW.tenant_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'material % not found for tenant %', NEW.material_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER stock_movements_apply
    AFTER INSERT ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION cineatelie.apply_stock_movement();

CREATE TRIGGER stock_movements_immutable
    BEFORE UPDATE OR DELETE ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION cineatelie.forbid_write();

-- audit_log is append-only, but the retention sweep must be able to prune aged rows.
CREATE TRIGGER audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION cineatelie.append_only_with_retention();

-- Record every Kanban stage transition without the application having to remember.
CREATE OR REPLACE FUNCTION cineatelie.log_service_order_stage()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.stage IS NOT DISTINCT FROM OLD.stage THEN
        RETURN NEW;
    END IF;
    INSERT INTO service_order_stage_history
        (tenant_id, service_order_id, from_stage, to_stage, reason, changed_by)
    VALUES
        (NEW.tenant_id, NEW.id,
         CASE WHEN TG_OP = 'UPDATE' THEN OLD.stage END,
         NEW.stage,
         CASE WHEN NEW.stage = 'cancelado' THEN NEW.cancellation_reason END,
         cineatelie.current_user_id());
    NEW.stage_changed_at := now();
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER service_orders_stage_log
    BEFORE INSERT OR UPDATE OF stage ON service_orders
    FOR EACH ROW EXECUTE FUNCTION cineatelie.log_service_order_stage();

-- -------------------------------------------------------------------------------------
-- 14. Views and functions used by the UI
-- -------------------------------------------------------------------------------------

-- Reserved vs. available stock, as shown in the quote editor ("Mat. em estoque").
CREATE OR REPLACE VIEW v_material_availability AS
SELECT
    m.tenant_id,
    m.id                                                  AS material_id,
    m.name,
    m.sku,
    m.unit,
    m.quantity_on_hand,
    COALESCE(r.reserved_quantity, 0)                      AS reserved_quantity,
    m.quantity_on_hand - COALESCE(r.reserved_quantity, 0) AS available_quantity,
    m.minimum_quantity,
    (m.quantity_on_hand - COALESCE(r.reserved_quantity, 0)) <= m.minimum_quantity AS is_low_stock,
    m.current_unit_cost,
    m.storage_location
FROM materials m
LEFT JOIN (
    SELECT tenant_id, material_id, SUM(quantity) AS reserved_quantity
      FROM stock_reservations
     WHERE status = 'reserved'
     GROUP BY tenant_id, material_id
) r ON r.material_id = m.id AND r.tenant_id = m.tenant_id
WHERE m.deleted_at IS NULL;

-- Ready-to-wear availability. A unit is OCCUPIED while it is reserved or confirmed for
-- any transaction, and additionally while it is out on an active rental. A completed sale
-- instead decrements units_total, because the piece has left permanently.
CREATE OR REPLACE VIEW v_rtw_availability AS
SELECT
    i.tenant_id,
    i.id                                        AS item_id,
    i.name,
    i.category,
    i.size_label,
    i.units_total,
    COALESCE(o.units_occupied, 0)               AS units_occupied,
    i.units_total - COALESCE(o.units_occupied, 0) AS units_available,
    i.sale_price,
    i.rental_price,
    (SELECT a.thumbnail_path FROM attachments a
      WHERE a.ready_to_wear_item_id = i.id AND a.deleted_at IS NULL
      ORDER BY a.sort_order LIMIT 1) AS thumbnail_path
FROM ready_to_wear_items i
LEFT JOIN (
    -- Occupancy is derived from the ordinary quote -> order pipeline; ready-to-wear has no
    -- transaction table of its own (see the database spec, "Sale and rental are quotes").
    --
    -- A unit is occupied while its quote line is live:
    --   * the quote is not refused / expired / cancelled, AND
    --   * either no service order exists yet (still just committed), or the order is not
    --     cancelled AND has not yet released the unit —
    --       venda   : released at delivery, because units_total is decremented instead
    --       aluguel : released when rental_returned_at is set, not at delivery
    SELECT qi.tenant_id,
           qi.ready_to_wear_item_id AS item_id,
           SUM(qi.quantity)         AS units_occupied
      FROM quote_items qi
      JOIN quotes q
        ON q.id = qi.quote_id AND q.tenant_id = qi.tenant_id
      LEFT JOIN service_orders so
        ON so.quote_id = q.id AND so.tenant_id = q.tenant_id AND so.deleted_at IS NULL
     WHERE qi.ready_to_wear_item_id IS NOT NULL
       AND q.deleted_at IS NULL
       AND q.status NOT IN ('recusado','expirado','cancelado')
       AND (
             so.id IS NULL
             OR ( so.stage <> 'cancelado'
                  AND CASE so.order_type
                        WHEN 'venda'   THEN so.stage <> 'entregue'
                        WHEN 'aluguel' THEN so.rental_returned_at IS NULL
                        ELSE true
                      END )
           )
     GROUP BY qi.tenant_id, qi.ready_to_wear_item_id
) o ON o.item_id = i.id AND o.tenant_id = i.tenant_id
WHERE i.deleted_at IS NULL;

-- Storage consumed per tenant, in bytes and in attachment count. Checked against the
-- storage_mb_total quota on every upload authorisation (BR-ATT-02). Soft-deleted rows
-- still count until the sweep removes the object, which is deliberate: the space is
-- genuinely still occupied.
CREATE OR REPLACE VIEW v_tenant_storage_usage AS
SELECT
    t.id                                        AS tenant_id,
    COALESCE(a.attachment_count, 0)             AS attachment_count,
    COALESCE(a.bytes_used, 0)                   AS bytes_used,
    round(COALESCE(a.bytes_used, 0) / 1048576.0, 2) AS megabytes_used
FROM tenants t
LEFT JOIN (
    SELECT tenant_id, count(*) AS attachment_count, SUM(size_bytes) AS bytes_used
      FROM attachments
     WHERE deleted_at IS NULL
     GROUP BY tenant_id
) a ON a.tenant_id = t.id
WHERE t.deleted_at IS NULL;

-- Active entitlement window per tenant: the single source of truth for access control.
CREATE OR REPLACE VIEW v_active_subscription AS
SELECT DISTINCT ON (s.tenant_id)
    s.tenant_id,
    s.id AS subscription_id,
    s.plan_code,
    s.status,
    s.starts_on,
    s.ends_on,
    s.grace_days,
    (CURRENT_DATE BETWEEN s.starts_on AND (s.ends_on + s.grace_days)) AS is_within_window
FROM subscriptions s
WHERE s.status IN ('trialing','active','past_due')
ORDER BY s.tenant_id, s.ends_on DESC;

-- Cash-flow rows for one month, with unpaid earlier entries carried forward (BR-FIN-03).
-- Usage: SELECT * FROM fn_cash_flow_month(:tenant_id, DATE '2026-09-01');
CREATE OR REPLACE FUNCTION fn_cash_flow_month(p_tenant_id uuid, p_month_start date)
RETURNS TABLE (
    entry_id     uuid,
    direction    text,
    description  text,
    amount       numeric(14,2),
    due_date     date,
    paid_on      date,
    status       text,
    is_overdue   boolean,
    bucket       text            -- 'do_mes' | 'em_atraso'
)
LANGUAGE sql
STABLE
AS $fn$
    SELECT
        e.id, e.direction, e.description, e.amount, e.due_date, e.paid_on, e.status,
        (e.status IN ('previsto','confirmado') AND e.due_date < p_month_start) AS is_overdue,
        CASE WHEN e.due_date < p_month_start THEN 'em_atraso' ELSE 'do_mes' END AS bucket
    FROM finance_entries e
    WHERE e.tenant_id = p_tenant_id
      AND e.deleted_at IS NULL
      AND e.status <> 'cancelado'
      AND (
            (e.due_date >= p_month_start AND e.due_date < (p_month_start + interval '1 month'))
         OR (e.due_date <  p_month_start AND e.status IN ('previsto','confirmado'))
          )
    ORDER BY e.due_date, e.created_at;
$fn$;

-- -------------------------------------------------------------------------------------
-- 15. Database roles and Row-Level Security (ADR-001)
-- -------------------------------------------------------------------------------------

-- Application login role. NOT the owner of the tables, so RLS always applies to it.
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator NOLOGIN;
    END IF;
END
$do$;

GRANT USAGE ON SCHEMA cineatelie TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA cineatelie TO app_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA cineatelie TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA cineatelie
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;

-- `extensions` on the search_path (section 0) is not enough on its own: Postgres also
-- gates schema access by USAGE, independently of whether the schema resolves via
-- search_path — confirmed the hard way, by the isolation suite, on Supabase specifically:
-- `gen_random_bytes()` kept failing with "does not exist" even after search_path was
-- fixed, because app_user had no USAGE on the schema pgcrypto actually lives in there.
-- Conditional because `extensions` does not exist at all locally (plain `postgres:15` has
-- no such schema) — an unconditional GRANT would fail the baseline outright there.
--
-- app_worker is deliberately not listed: it does not exist yet at this point in the
-- script (created in section 16, below) and inherits this the same way it inherits every
-- other grant made to app_user here — via `IN ROLE app_user`, not a separate GRANT.
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'extensions') THEN
        GRANT USAGE ON SCHEMA extensions TO app_user, app_migrator;
    END IF;
END
$do$;

-- Reference tables are read-only for the application.
REVOKE INSERT, UPDATE, DELETE ON roles, permissions, role_permissions, quotas, plan_quotas,
       features, plans, plan_features, measurement_fields FROM app_user;

-- Enable RLS + the tenant policy on every tenant-scoped table.
DO $do$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'memberships','tenant_invitations','subscriptions','tenant_feature_overrides',
        'billing_invoices','tenant_quota_overrides','tenant_settings','document_counters',
        'card_fees','clients',
        'client_measurements','service_types','suppliers','materials','material_cost_history',
        'stock_movements','stock_reservations','ready_to_wear_items','quotes','quote_items',
        'quote_drafts','service_orders','service_order_stage_history','attachments',
        'appointments','finance_categories','finance_entries','contract_templates','receipts',
        'receipt_items','lgpd_requests','idempotency_records','notifications',
        'rtw_movements','rtw_price_history'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
        EXECUTE format($pol$
            CREATE POLICY %1$I_tenant_isolation ON %1$I
            FOR ALL
            TO app_user
            USING      (tenant_id = cineatelie.current_tenant_id())
            WITH CHECK (tenant_id = cineatelie.current_tenant_id())
        $pol$, t);
    END LOOP;
END
$do$;

-- audit_log and job_queue carry a NULLABLE tenant_id, because platform-level rows exist
-- (the keep-alive ping, the subscription sweep, a signup audited before any tenant exists).
-- The uniform policy above would make those rows invisible to every session, including the
-- worker's, so these two get a policy that also admits the platform-scoped rows.
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE  ROW LEVEL SECURITY;
CREATE POLICY audit_log_tenant_isolation ON audit_log
    FOR ALL TO app_user
    USING      (tenant_id IS NULL OR tenant_id = cineatelie.current_tenant_id())
    WITH CHECK (tenant_id IS NULL OR tenant_id = cineatelie.current_tenant_id());

ALTER TABLE job_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_queue FORCE  ROW LEVEL SECURITY;
CREATE POLICY job_queue_tenant_isolation ON job_queue
    FOR ALL TO app_user
    USING      (tenant_id IS NULL OR tenant_id = cineatelie.current_tenant_id())
    WITH CHECK (tenant_id IS NULL OR tenant_id = cineatelie.current_tenant_id());
COMMENT ON POLICY job_queue_tenant_isolation ON job_queue IS
  'Platform-scoped jobs (tenant_id IS NULL) are visible to any session. The worker must still '
  'SET LOCAL app.current_tenant_id before executing a tenant-scoped job, so the job body runs '
  'under that tenant''s RLS.';

-- tenants: a row is visible only when it IS the session tenant.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenants_self_isolation ON tenants
    FOR ALL TO app_user
    USING      (id = cineatelie.current_tenant_id())
    WITH CHECK (id = cineatelie.current_tenant_id());

-- users / user_identities: a user only ever reads their own row. Team listings are
-- served through the SECURITY DEFINER function below.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE  ROW LEVEL SECURITY;
CREATE POLICY users_self ON users
    FOR ALL TO app_user
    USING      (id = cineatelie.current_user_id())
    WITH CHECK (id = cineatelie.current_user_id());

ALTER TABLE user_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_identities FORCE  ROW LEVEL SECURITY;
CREATE POLICY user_identities_self ON user_identities
    FOR ALL TO app_user
    USING      (user_id = cineatelie.current_user_id())
    WITH CHECK (user_id = cineatelie.current_user_id());

-- Team directory for Configurações > Equipe, scoped to the session tenant.
CREATE OR REPLACE FUNCTION fn_tenant_members(p_tenant_id uuid)
RETURNS TABLE (
    membership_id uuid, user_id uuid, full_name text, email citext,
    role_code text, role_label text, status text, accepted_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = cineatelie, extensions
AS $fn$
    SELECT m.id, u.id, u.full_name, u.email, m.role_code, r.label_pt_br, m.status, m.accepted_at
      FROM memberships m
      JOIN users u ON u.id = m.user_id
      JOIN roles r ON r.code = m.role_code
     WHERE m.tenant_id = p_tenant_id
       AND p_tenant_id = cineatelie.current_tenant_id()   -- refuse to leak another tenant's team
       AND m.status <> 'revoked'
     ORDER BY r.sort_order, u.full_name;
$fn$;

-- Pre-tenant lookup: which workspaces may this user enter? Runs before
-- app.current_tenant_id is known, so it is keyed on app.current_user_id only.
CREATE OR REPLACE FUNCTION fn_my_tenants()
RETURNS TABLE (
    tenant_id uuid, slug text, trade_name text, logo_url text,
    role_code text, is_default boolean, plan_code text, subscription_active boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = cineatelie, extensions
AS $fn$
    SELECT t.id, t.slug, t.trade_name, t.logo_url, m.role_code, m.is_default,
           s.plan_code, COALESCE(s.is_within_window, false)
      FROM memberships m
      JOIN tenants t ON t.id = m.tenant_id
      LEFT JOIN v_active_subscription s ON s.tenant_id = t.id
     WHERE m.user_id = cineatelie.current_user_id()
       AND m.status = 'active'
       AND t.deleted_at IS NULL
     ORDER BY m.is_default DESC, t.trade_name;
$fn$;

-- Login-time identity resolution (ADR-001 addendum, 2026-09-27; ADR-006 BR-ID-01): given a
-- verified JWT's (provider, subject), which platform user_id does it belong to? Runs before
-- app.current_user_id is known -- that is exactly why it must be SECURITY DEFINER:
-- user_identities_self's own policy is keyed on current_user_id(), so an ordinary app_user
-- query can never see the very row needed to learn what to set current_user_id() to. Takes
-- only a (provider, provider_subject) pair -- never anything from a request body -- and
-- returns only a uuid, matching fn_tenant_members/fn_my_tenants's shape and risk profile
-- exactly.
CREATE OR REPLACE FUNCTION fn_resolve_user_identity(p_provider text, p_provider_subject text)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = cineatelie, extensions
AS $fn$
    SELECT user_id
      FROM user_identities
     WHERE provider = p_provider
       AND provider_subject = p_provider_subject;
$fn$;

REVOKE EXECUTE ON FUNCTION fn_tenant_members(uuid), fn_my_tenants(), fn_resolve_user_identity(text, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION fn_tenant_members(uuid), fn_my_tenants(), fn_resolve_user_identity(text, text) TO app_user;

-- -------------------------------------------------------------------------------------
-- 16. Session defaults / non-blocking guardrails (ADR-003)
-- -------------------------------------------------------------------------------------
-- The pool also applies these per connection, so they survive a role change.
-- Values are overridable per environment (see the architecture appendix).
ALTER ROLE app_user SET statement_timeout                   = '5000ms';
ALTER ROLE app_user SET lock_timeout                        = '3000ms';
ALTER ROLE app_user SET idle_in_transaction_session_timeout = '10000ms';
ALTER ROLE app_user SET search_path                         = cineatelie, extensions;
-- app_migrator has no statement/lock timeout override (DDL legitimately runs long,
-- database spec table "app_migrator | ... | none (DDL)"), but it does need this, so a
-- *future* migration's unqualified CREATE TABLE also lands in cineatelie by default.
ALTER ROLE app_migrator SET search_path                    = cineatelie, extensions;

-- Background workers legitimately run longer than an HTTP request.
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        CREATE ROLE app_worker NOLOGIN IN ROLE app_user;
    END IF;
END
$do$;
ALTER ROLE app_worker SET statement_timeout = '120000ms';
ALTER ROLE app_worker SET lock_timeout      = '10000ms';
ALTER ROLE app_worker SET search_path       = cineatelie, extensions;

-- Partition maintenance is the worker's job, and only through these SECURITY DEFINER
-- functions — app_worker owns no partition and holds no CREATE on the schema.
GRANT EXECUTE ON FUNCTION cineatelie.ensure_audit_log_partitions(int, int)   TO app_worker;
GRANT EXECUTE ON FUNCTION cineatelie.drop_audit_log_partitions_before(date)  TO app_worker;

-- -------------------------------------------------------------------------------------
-- 17. Reference / seed data (tenant-agnostic)
-- -------------------------------------------------------------------------------------

INSERT INTO roles (code, label_pt_br, description, sort_order) VALUES
  ('owner',      'Proprietária', 'Full access to the workspace, billing and team.',  10),
  ('finance',    'Financeiro',   'Cash flow, quotes, clients and contracts.',        20),
  ('seamstress', 'Costureira',   'Production board, stock, services and suppliers.', 30),
  ('front_desk', 'Atendimento',  'Quotes, clients, services and ready-to-wear.',     40)
ON CONFLICT (code) DO NOTHING;

INSERT INTO permissions (code, resource, action, description) VALUES
  ('dashboard:read','dashboard','read','View the overview dashboard'),
  ('quotes:read','quotes','read','List and open quotes'),
  ('quotes:write','quotes','write','Create and edit quotes'),
  ('quotes:approve','quotes','manage','Approve or refuse a quote'),
  ('clients:read','clients','read','List and open clients'),
  ('clients:write','clients','write','Create and edit clients and measurement sheets'),
  ('clients:delete','clients','delete','Remove a client (LGPD erasure)'),
  ('services:read','services','read','View the service catalogue'),
  ('services:write','services','write','Maintain the service catalogue'),
  ('stock:read','stock','read','View stock and inputs'),
  ('stock:write','stock','write','Register stock entries and adjustments'),
  ('suppliers:read','suppliers','read','View suppliers'),
  ('suppliers:write','suppliers','write','Maintain suppliers'),
  ('rtw:read','ready_to_wear','read','View ready-to-wear pieces'),
  ('rtw:write','ready_to_wear','write','Maintain ready-to-wear pieces'),
  ('orders:read','service_orders','read','View the production board'),
  ('orders:write','service_orders','write','Move cards, edit orders, attach files'),
  ('agenda:read','agenda','read','View the calendar'),
  ('agenda:write','agenda','write','Create and edit appointments'),
  ('finance:read','finance','read','View cash flow'),
  ('finance:write','finance','write','Create and settle cash-flow entries'),
  ('contracts:read','contracts','read','View and print contracts and receipts'),
  ('contracts:write','contracts','write','Edit contract templates'),
  ('settings:read','settings','read','View workspace settings'),
  ('settings:write','settings','write','Change workspace settings and card fees'),
  ('team:manage','team','manage','Invite, change role and revoke members'),
  ('billing:manage','billing','manage','Manage the subscription and invoices')
ON CONFLICT (code) DO NOTHING;

-- owner = every permission
INSERT INTO role_permissions (role_code, permission_code)
SELECT 'owner', code FROM permissions
ON CONFLICT DO NOTHING;

INSERT INTO role_permissions (role_code, permission_code) VALUES
  ('finance','dashboard:read'),('finance','quotes:read'),('finance','quotes:write'),
  ('finance','clients:read'),('finance','finance:read'),('finance','finance:write'),
  ('finance','contracts:read'),
  ('seamstress','dashboard:read'),('seamstress','orders:read'),('seamstress','orders:write'),
  ('seamstress','stock:read'),('seamstress','stock:write'),('seamstress','services:read'),
  ('seamstress','rtw:read'),('seamstress','suppliers:read'),('seamstress','agenda:read'),
  ('front_desk','dashboard:read'),('front_desk','quotes:read'),('front_desk','quotes:write'),
  ('front_desk','clients:read'),('front_desk','clients:write'),('front_desk','services:read'),
  ('front_desk','rtw:read'),('front_desk','agenda:read'),('front_desk','agenda:write')
ON CONFLICT DO NOTHING;

INSERT INTO features (key, label_pt_br, description) VALUES
  ('dashboard','Visão geral','Overview dashboard'),
  ('quotes','Orçamentos','Quote editor and pipeline'),
  ('clients','Clientes','Client registry and measurement sheet'),
  ('services','Serviços','Service catalogue'),
  ('stock','Estoque e insumos','Materials, movements and cost history'),
  ('suppliers','Fornecedores','Supplier registry'),
  ('ready_to_wear','Pronta entrega','Ready-to-wear sale and rental'),
  ('orders','Ordens de serviço','Production Kanban and finished-order history'),
  ('agenda','Agenda','Calendar and appointments'),
  ('finance','Fluxo de caixa','Receivables and payables'),
  ('contracts','Contratos','Contract and receipt templates'),
  ('team','Equipe','Multi-user access with roles'),
  ('attachments','Anexos','File attachments on orders'),
  ('courses','Cursos','Roadmap: training content'),
  ('whatsapp','WhatsApp','Roadmap: WhatsApp messaging'),
  ('email_relay','E-mail','Roadmap: transactional e-mail relay'),
  ('open_finance','Open Finance','Roadmap: bank feed integration'),
  ('ai_assistant','Assistente IA','Roadmap: AI assistants')
ON CONFLICT (key) DO NOTHING;

-- =====================================================================================
-- Plans — ONE paid plan at launch (OI-01, decided 2026-09-25).
-- =====================================================================================
-- A single "Starter" plan with every shipped feature enabled, plus a 7-day trial of the
-- same thing. A second tier was considered and dropped: its only differentiators would
-- have been team seats and attachments, which is too thin to price separately and tends
-- to confuse buyers rather than upsell them.
--
-- Adding a tier later is two INSERTs (a `plans` row and its `plan_features` rows) — no
-- schema change, no deploy. It is NOT seeded here as a dormant placeholder, because a
-- visible-but-unused plan invites exactly the confusion the decision avoids.
--
-- price_amount is 0 on purpose: the price is still undecided. Changing it later is one
-- UPDATE, and `subscriptions.price_amount` snapshots what each tenant actually agreed to
-- pay, so repricing never rewrites history and existing subscribers are grandfathered.
-- =====================================================================================
INSERT INTO plans (code, label_pt_br, description, price_amount, billing_period, trial_days, sort_order) VALUES
  ('trial',  'Avaliação', 'Avaliação de 7 dias, com todos os recursos do plano Starter.', 0, 'annual', 7,  5),
  ('starter','Starter',   'Gestão completa do ateliê.',                                   0, 'annual', 0, 10)
ON CONFLICT (code) DO NOTHING;

-- Every shipped feature is enabled on both plans. Only the roadmap features — which have
-- no implementation behind them yet — are off; they are seeded so the gate exists the day
-- the feature lands (ADR-006).
INSERT INTO plan_features (plan_code, feature_key, is_enabled, limit_value)
SELECT p.code,
       f.key,
       f.key NOT IN ('courses','whatsapp','email_relay','open_finance','ai_assistant'),
       -- The one place where "all features" carries a number. 5 seats is generous for a
       -- micro-atelier while preserving a future pricing lever. RAISING it later is free;
       -- LOWERING it would take something away from existing tenants and would need
       -- grandfathering via tenant_feature_overrides. See OI-01 note (d).
       CASE WHEN f.key = 'team' THEN 5 END
FROM plans p CROSS JOIN features f
ON CONFLICT DO NOTHING;

-- =====================================================================================
-- Quota catalogue and the Starter ceilings.
-- =====================================================================================
-- The numbers below are the working assumption behind the Starter price (OI-13). They are
-- sized against the Supabase free tier — 1 GB file storage, 500 MB database, 5 GB egress
-- per month — so that one tenant on Starter cannot exhaust it alone. Raise them per tenant
-- with tenant_quota_overrides, or per plan when a paid tier exists; raising is always safe,
-- lowering takes something away (see architecture, "Changing packaging later").
INSERT INTO quotas (key, label_pt_br, unit, description) VALUES
  ('attachments_per_item', 'Anexos por item', 'count',
   'Attachments on ONE service order or ONE ready-to-wear piece. The number the user actually feels.'),
  ('attachments_total',    'Total de anexos', 'count',
   'Attachments across the whole workspace. The marketed ceiling and the pricing lever.'),
  ('max_attachment_mb',    'Tamanho máximo por arquivo', 'megabytes',
   'Per-file ceiling. With attachments_total, this is what bounds worst-case storage: total x max.'),
  ('storage_mb_total',     'Espaço total de arquivos', 'megabytes',
   'Safety ceiling only, NOT the marketed quota. NULL on every plan because attachments_total x '
   'max_attachment_mb already bounds the cost. Set it per tenant only to stop a pathological case.')
ON CONFLICT (key) DO NOTHING;

-- Tier ceilings (owner decision, 2026-09-26). Counts, not megabytes: a count is what the
-- user understands and what the plan is sold on, while max_attachment_mb quietly bounds the
-- cost — worst case is attachments_total x max_attachment_mb, which is predictable.
--
--   trial   :  1 per item,   10 total  ->  worst case   50 MB
--   starter :  3 per item,  300 total  ->  worst case  1.5 GB, realistic ~600 MB at 2 MB/file
--   pro     : 10 per item, 1500 total  ->  priced to cover its own storage
--
-- 'pro' is NOT seeded — there is no Pro plan yet (OI-01). Its numbers are recorded here so the
-- shape of the upgrade is decided even though the plan is not.
INSERT INTO plan_quotas (plan_code, quota_key, limit_value) VALUES
  ('trial',   'attachments_per_item',     1),
  ('trial',   'attachments_total',       10),
  ('trial',   'max_attachment_mb',        5),
  ('trial',   'storage_mb_total',      NULL),
  ('starter', 'attachments_per_item',     3),
  ('starter', 'attachments_total',      300),
  ('starter', 'max_attachment_mb',        5),
  ('starter', 'storage_mb_total',      NULL)
ON CONFLICT DO NOTHING;

-- =====================================================================================
-- The 42 fields of page 1 of the paper measurement sheet.
-- =====================================================================================
-- CALIBRATED 2026-09-24 (build task BT-01, closed).
--
-- Both `top_pct` and `label_pt_br` are taken VERBATIM from the design prototype
-- `01-product/design_handoff_cine_atelie/Ficha de Medidas.dc.html`, which carries the
-- designer's own tuned values in each field's `top:` CSS property. They were then
-- cross-checked against the croqui images themselves:
--
--     landmark in croqui-frente.png   value    field
--     shoulder line      ~15-17%      15.3     ombroOmbroF
--     bust apex          ~24-26%      23.8     lBusto
--     waist              ~33-34%      33.5     lCintura
--     hip                ~44-45%      45.3     lQuadril
--     knee               ~65%         65.2     lJoelho
--     ankle              ~89-92%      89.2     lTornozelo
--     gluteal fold (costas) ~47%      47.0     altGancho
--
-- `top_pct` is the vertical CENTRE of the field row, not its top edge: the prototype
-- applies `transform: translateY(-50%)` to every row. A renderer that treats it as the
-- top edge will sit every field ~12 px low.
--
-- Labels are the prototype's pt-BR copy exactly, including its own capitalisation
-- ("Altura da manga", "Cava a cava Frente"). The handoff declares copy final; do not
-- normalise it.
-- =====================================================================================
INSERT INTO measurement_fields
  (key, panel, column_side, sort_order, label_pt_br, value_type, unit, placeholder, top_pct) VALUES
  -- Panel 1 — Frente · larguras e comprimentos (left column: label before input)
  ('ombroOmbroF','front_widths','left', 10,'Ombro a Ombro Frente','decimal','cm','cm',15.300),
  ('lBiceps','front_widths','left', 20,'Largura do Bíceps','decimal','cm','cm',24.300),
  ('altCotovelo','front_widths','left', 30,'Altura do Cotovelo','decimal','cm','cm',31.300),
  ('lCotovelo','front_widths','left', 40,'Largura do Cotovelo','decimal','cm','cm',37.300),
  ('lPunho','front_widths','left', 50,'Largura do Punho','decimal','cm','cm',44.300),
  ('altManga','front_widths','left', 60,'Altura da manga','decimal','cm','cm',51.300),
  ('compSaia','front_widths','left', 70,'Comprimento da Saia','decimal','cm','cm',62.300),
  ('lTornozelo','front_widths','left', 80,'Largura do Tornozelo','decimal','cm','cm',89.200),
  ('compVestido','front_widths','left', 90,'Comprimento Total do Vestido','decimal','cm','cm',95.900),
  -- Panel 1 (right column: input before label)
  ('lPescoco','front_widths','right', 10,'Largura do Pescoço','decimal','cm','cm',12.300),
  ('cavaCavaF','front_widths','right', 20,'Cava a cava Frente','decimal','cm','cm',19.300),
  ('lBusto','front_widths','right', 30,'Largura do Busto','decimal','cm','cm',23.800),
  ('lAbaixoBusto','front_widths','right', 40,'Largura do Abaixo do Busto','decimal','cm','cm',27.500),
  ('lCintura','front_widths','right', 50,'Largura da Cintura','decimal','cm','cm',33.500),
  ('altQuadril','front_widths','right', 60,'Altura do Quadril','decimal','cm','cm',42.000),
  ('lQuadril','front_widths','right', 70,'Largura do Quadril','decimal','cm','cm',45.300),
  ('l10Quadril','front_widths','right', 80,'Largura de 10cm Abaixo do Quadril','decimal','cm','cm',48.700),
  ('l15Quadril','front_widths','right', 90,'Largura de 15cm Abaixo do Quadril','decimal','cm','cm',52.000),
  ('lCoxa','front_widths','right',100,'Largura da Coxa','decimal','cm','cm',61.200),
  ('lJoelho','front_widths','right',110,'Largura do Joelho','decimal','cm','cm',65.200),
  ('altJoelho','front_widths','right',120,'Altura do Joelho','decimal','cm','cm',68.700),
  -- Panel 2 — Frente · alturas (left)
  ('transversalF','front_heights','left', 10,'Transversal Frente','decimal','cm','cm',13.600),
  ('ombro','front_heights','left', 20,'Ombro','decimal','cm','cm',18.200),
  ('entresseio','front_heights','left', 30,'Entresseio','decimal','cm','cm',24.000),
  ('lBustoF','front_heights','left', 40,'Largura do Busto Frente','decimal','cm','cm',29.000),
  ('raioBusto','front_heights','left', 50,'Raio do Busto','decimal','cm','cm',34.000),
  -- Panel 2 (right)
  ('altFrente','front_heights','right', 10,'Altura do Corpo Frente','decimal','cm','cm',11.000),
  ('centroF','front_heights','right', 20,'Altura do Centro Frente','decimal','cm','cm',16.000),
  ('altCavaF','front_heights','right', 30,'Altura da Cava','decimal','cm','cm',21.000),
  ('altBusto','front_heights','right', 40,'Altura do Busto','decimal','cm','cm',26.000),
  ('altAbaixoBusto','front_heights','right', 50,'Altura do Abaixo do Busto','decimal','cm','cm',31.000),
  ('altLateral','front_heights','right', 60,'Altura da Lateral do Corpo','decimal','cm','cm',37.000),
  -- Panel 3 — Costas (left)
  ('ombroOmbroC','back','left', 10,'Ombro a Ombro Costas','decimal','cm','cm',17.100),
  ('cavaCavaC','back','left', 20,'Cava a Cava Costas','decimal','cm','cm',23.300),
  -- Panel 3 (right)
  ('transversalC','back','right', 10,'Transversal Costas','decimal','cm','cm',13.000),
  ('altCosta','back','right', 20,'Altura do Corpo Costas','decimal','cm','cm',18.000),
  ('centroC','back','right', 30,'Altura do Centro Costas','decimal','cm','cm',23.000),
  ('altCavaC','back','right', 40,'Altura da Cava Costas','decimal','cm','cm',27.600),
  ('curvaturaLombar','back','right', 50,'Curvatura da Lombar','text',NULL,'P, M, G',35.300),
  ('altGancho','back','right', 60,'Altura do Gancho','decimal','cm','cm',47.000),
  ('altLateralPerna','back','right', 70,'Altura da Lateral da Perna','decimal','cm','cm',51.700),
  ('altEntrepernas','back','right', 80,'Altura do Entrepernas','decimal','cm','cm',56.700)
ON CONFLICT (key) DO NOTHING;

COMMIT;

-- =====================================================================================
-- Per-tenant provisioning template
-- Executed by the backend inside a single transaction when a workspace is created.
-- Kept here as executable documentation; see spec-20260920-backend.md, WF-01.
-- =====================================================================================
-- INSERT INTO tenant_settings (tenant_id) VALUES (:tenant_id);
--
-- INSERT INTO document_counters (tenant_id, document_type, prefix, padding) VALUES
--   (:tenant_id,'quote','ORC',4),
--   (:tenant_id,'service_order','OS',4),
--   (:tenant_id,'receipt','REC',4),
--   (:tenant_id,'contract','CTR',4);
--
-- INSERT INTO card_fees (tenant_id, installments, fee_pct) VALUES
--   (:tenant_id, 1, 3.15),(:tenant_id, 2, 4.79),(:tenant_id, 3, 6.12),(:tenant_id, 4, 7.42),
--   (:tenant_id, 6, 8.80),(:tenant_id,10,12.50),(:tenant_id,12,14.20);
--
-- INSERT INTO finance_categories (tenant_id, name, direction, is_system) VALUES
--   (:tenant_id,'Ordem de serviço entregue','receita',true),
--   (:tenant_id,'Venda de pronta entrega','receita',true),
--   (:tenant_id,'Aluguel de peça','receita',true),
--   (:tenant_id,'Compra de material','despesa',true),
--   (:tenant_id,'Taxas de cartão','despesa',true),
--   (:tenant_id,'Retirada (pró-labore)','despesa',true),
--   (:tenant_id,'Manutenção','despesa',true),
--   (:tenant_id,'Multa por atraso','receita',true),      -- rental returned late (BR-RTW-08)
--   (:tenant_id,'Avaria','receita',true),                -- rental returned damaged (BR-RTW-08)
--   (:tenant_id,'Assinatura da plataforma','despesa',true);
--
-- INSERT INTO contract_templates (tenant_id, document_type, title, body_text, is_ready) VALUES
--   (:tenant_id,'sob_medida','Contrato de prestação de serviços — confecção sob medida', :body_sob_medida, true),
--   (:tenant_id,'ajuste_conserto','Recibo de serviços — ajustes e consertos', :body_ajustes, true),
--   (:tenant_id,'venda','Contrato de compra e venda — peça de pronta entrega', :body_venda, false),
--   (:tenant_id,'aluguel','Contrato de locação — peça de pronta entrega', :body_aluguel, false);
-- NOTE: venda and aluguel seed with is_ready = false. The prototype marks both
-- "[Em desenvolvimento]" — the clauses (garantia, trocas, caução, multa por atraso,
-- avaria) are not written yet. They became first-class document types when sale and
-- rental joined the quote pipeline, so the templates are now a launch dependency for
-- those two quote types.
--
-- INSERT INTO subscriptions (tenant_id, plan_code, status, starts_on, ends_on, trial_ends_on)
--   VALUES (:tenant_id,'trial','trialing', CURRENT_DATE, CURRENT_DATE + 7, CURRENT_DATE + 7);
