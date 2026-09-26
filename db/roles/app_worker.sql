-- =====================================================================================
-- app_worker — the role the background worker connects as (ADR-003, ADR-010)
-- =====================================================================================
-- Same pattern as app_user.sql (see its comment for why this exists alongside the
-- baseline migration's own APP_WORKER_PASSWORD handling). app_worker's grants (membership
-- in app_user, the longer statement_timeout, the two partition-management functions) are
-- entirely defined in the baseline — this file must never duplicate or override them.
--
--   psql "<admin connection string>" -v password='<real password from your secret manager>' \
--        -f db/roles/app_worker.sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        RAISE EXCEPTION 'app_worker does not exist yet — run the baseline migration first';
    END IF;
END
$$;

ALTER ROLE app_worker LOGIN PASSWORD :'password';
