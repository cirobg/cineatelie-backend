-- =====================================================================================
-- app_migrator — the role that runs schema migrations (ADR-009, ADR-017; database spec §8.1)
-- =====================================================================================
-- Run ONCE per new environment, by whatever admin connection the provider gives you
-- (Supabase's default project role; a local superuser). Never by CI, never by the app.
--
-- Local Docker Compose does not need this file: docker-compose.yml sets
-- POSTGRES_USER=app_migrator, so the official postgres image creates app_migrator as the
-- bootstrap superuser automatically, already with LOGIN. This script is for every OTHER
-- environment (Supabase, a bare PostgreSQL instance), where app_migrator does not exist yet
-- and the baseline migration (db/baseline/00001_baseline.sql) is about to run AS this role.
--
-- Usage: psql "<admin connection string>" -v password='<real password from your secret
--        manager>' -f db/roles/app_migrator.sql
--
-- After this runs, point MIGRATION_DATABASE_URL at app_migrator and run
-- `alembic upgrade head`. The baseline migration's own role setup (schema section 15) is
-- guarded by `IF NOT EXISTS`, so it will see app_migrator already exists and leave it alone.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator LOGIN PASSWORD :'password' CREATEROLE;
    ELSE
        ALTER ROLE app_migrator LOGIN PASSWORD :'password' CREATEROLE;
    END IF;
END
$$;

-- app_migrator must be able to create the schema objects the baseline defines. On a fresh
-- database this is ownership of the target database; on a shared/managed instance (Supabase)
-- the provider's default role already owns it, so grant CREATE on the two schemas instead —
-- harmless if it turns out to be redundant with database ownership.
GRANT CREATE ON SCHEMA public TO app_migrator;
GRANT ALL ON SCHEMA public TO app_migrator;
