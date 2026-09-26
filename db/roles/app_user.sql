-- =====================================================================================
-- app_user — the role the running API connects as (ADR-001, ADR-015; database spec §8.1)
-- =====================================================================================
-- Run AFTER the baseline migration (revision 0001) has created app_user as NOLOGIN with
-- its grants and RLS policies. This script only adds a password so the role can be used
-- for real connections — it must never change what app_user is granted or owns; that is
-- entirely the baseline's job (schema section 15), reviewed once, in one place.
--
-- The baseline migration (db/versions/20260926_0001_baseline.py) already runs the
-- equivalent of this ALTER, once, if APP_USER_PASSWORD is set in the environment when it
-- runs — which is what makes local Docker Compose usable end to end with a single
-- `alembic upgrade head`. That only happens once, at the baseline's first run: use this
-- file instead whenever you need to set or rotate the password afterwards, deliberately,
-- without re-running migrations — in particular for every non-local environment, where
-- setting APP_USER_PASSWORD as a migration-time environment variable is not how secrets
-- are meant to flow. Run it once per deploy of a new database, by an admin connection:
--
--   psql "<admin connection string>" -v password='<real password from your secret manager>' \
--        -f db/roles/app_user.sql
--
-- Confirms nothing else about app_user changes: no CREATEDB, no CREATEROLE, no BYPASSRLS,
-- no table ownership (ADR-001's "the application role must not own tables").
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        RAISE EXCEPTION 'app_user does not exist yet — run the baseline migration first';
    END IF;
END
$$;

ALTER ROLE app_user LOGIN PASSWORD :'password';
