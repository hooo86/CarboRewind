-- Séparation des rôles PostgreSQL pour CarboRewind (à exécuter par un DBA, une fois).
--
--   carborewind_owner : propriétaire du schéma, utilisé UNIQUEMENT pour les migrations
--                       (alembic upgrade head), jamais par l'application en fonctionnement.
--   carborewind_app   : rôle de l'API et des workers ; DML sans DDL, et audit en ajout seul.
--
-- Usage : psql -v app_password="'…'" -f grants.sql carborewind
-- (le mot de passe est passé en variable psql, jamais écrit dans ce fichier).

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'carborewind_app') THEN
        CREATE ROLE carborewind_app LOGIN;
    END IF;
END $$;
ALTER ROLE carborewind_app PASSWORD :app_password;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO carborewind_app;

-- Lecture/écriture sur toutes les tables…
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO carborewind_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO carborewind_app;

-- …sauf l'audit : ajout et lecture seulement (le trigger refuse en plus UPDATE/DELETE/TRUNCATE).
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM carborewind_app;

-- La table de versions Alembic n'est modifiable que par le propriétaire.
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM carborewind_app;

-- Tables créées par les migrations futures (exécutées par carborewind_owner).
ALTER DEFAULT PRIVILEGES FOR ROLE carborewind_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO carborewind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE carborewind_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO carborewind_app;
