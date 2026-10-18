-- Ensure the integration-test database `admitlens_test` exists on the
-- local PostgreSQL 16 server, next to the development database.
--
-- Applied by `make db-up`, safe to run repeatedly.
--
-- Run with psql connected as POSTGRES_USER to POSTGRES_DB. The database is
-- owned by the connecting role. No roles or credentials are created here.

\set ON_ERROR_STOP on

-- CREATE DATABASE cannot run inside a transaction block or a DO block, so the
-- statement is built conditionally and executed by psql's \gexec. When the
-- database already exists the SELECT returns no rows and nothing runs.
-- template0 + UTF8 gives a clean database with the same encoding as the
-- development database; the locale is inherited from the cluster default.
SELECT 'CREATE DATABASE admitlens_test WITH TEMPLATE template0 ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'admitlens_test')
\gexec

-- Every new session on the test database starts in UTC (ADR-0010 §2.2).
-- The image default is 'Etc/UTC'; pin the canonical name so `SHOW timezone`
-- returns exactly 'UTC'. ALTER DATABASE ... SET is idempotent.
ALTER DATABASE admitlens_test SET timezone TO 'UTC';

-- Same for the development database this script is connected to, so a
-- direct psql session also reports 'UTC'. psql expands :"DBNAME" to the
-- current database name as a quoted identifier.
ALTER DATABASE :"DBNAME" SET timezone TO 'UTC';
