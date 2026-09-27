-- =====================================================================================
-- app_migrator — the role that runs schema migrations (ADR-009, ADR-017; database spec §8.1)
-- =====================================================================================
-- Run AFTER the baseline migration, as the same admin connection that ran it — see
-- db/README.md, "Roles". The baseline (schema section 15) already creates app_migrator
-- as NOLOGIN, with CREATEROLE and every grant it needs on the `cineatelie` schema, as a
-- normal side effect of applying schema section 15; nothing here needs to grant anything
-- itself. This script only adds a password, exactly like app_user.sql and app_worker.sql.
--
-- Local Docker Compose does not need this file at all: docker-compose.yml sets
-- POSTGRES_USER=app_migrator, so the official postgres image creates app_migrator as the
-- bootstrap superuser automatically, already with LOGIN.
--
-- Usage: psql "<admin connection string>" -v password='<real password from your secret
--        manager>' -f db/roles/app_migrator.sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        RAISE EXCEPTION 'app_migrator does not exist yet — run the baseline migration first';
    END IF;
END
$$;

ALTER ROLE app_migrator LOGIN PASSWORD :'password';
