--
-- PostgreSQL database cluster dump
--

\restrict rQuYRLQEm5eCtvgWfbUt4OUZljsbha4zmZn5uNqeG0Q7S7czStmWo2VXWvgZ5fc

SET default_transaction_read_only = off;

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

--
-- Roles
--

CREATE ROLE _aiven;
ALTER ROLE _aiven WITH SUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS;
CREATE ROLE _aiven_repl;
ALTER ROLE _aiven_repl WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN REPLICATION NOBYPASSRLS;
CREATE ROLE _aiven_rewind;
ALTER ROLE _aiven_rewind WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS;
CREATE ROLE _avnadmin_auditing;
ALTER ROLE _avnadmin_auditing WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB NOLOGIN NOREPLICATION NOBYPASSRLS;
CREATE ROLE _avnadmin_managed;
ALTER ROLE _avnadmin_managed WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB NOLOGIN NOREPLICATION NOBYPASSRLS;
CREATE ROLE _avnadmin_monitor;
ALTER ROLE _avnadmin_monitor WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS;
CREATE ROLE avnadmin;
ALTER ROLE avnadmin WITH NOSUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS;
CREATE ROLE avnadmin_group;
ALTER ROLE avnadmin_group WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB NOLOGIN NOREPLICATION NOBYPASSRLS;
CREATE ROLE postgres;
ALTER ROLE postgres WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS;

--
-- User Configurations
--

--
-- User Config "_aiven"
--

ALTER ROLE _aiven SET avn_wal_sender_stop_timeout TO '10s';
ALTER ROLE _aiven SET default_transaction_isolation TO 'read committed';
ALTER ROLE _aiven SET default_transaction_read_only TO 'off';
ALTER ROLE _aiven SET "pg_stat_monitor.pgsm_track" TO 'none';
ALTER ROLE _aiven SET "pg_stat_plans.track" TO 'none';
ALTER ROLE _aiven SET "pg_stat_statements.track" TO 'none';
ALTER ROLE _aiven SET "pgaudit.log" TO 'none';
ALTER ROLE _aiven SET "timescaledb.disable_load" TO 'true';

--
-- User Config "_aiven_repl"
--

ALTER ROLE _aiven_repl SET avn_wal_sender_stop_timeout TO '10s';
ALTER ROLE _aiven_repl SET default_transaction_isolation TO 'read committed';
ALTER ROLE _aiven_repl SET default_transaction_read_only TO 'off';
ALTER ROLE _aiven_repl SET "pg_stat_monitor.pgsm_track" TO 'none';
ALTER ROLE _aiven_repl SET "pg_stat_plans.track" TO 'none';
ALTER ROLE _aiven_repl SET "pg_stat_statements.track" TO 'none';
ALTER ROLE _aiven_repl SET "pgaudit.log" TO 'none';
ALTER ROLE _aiven_repl SET "timescaledb.disable_load" TO 'true';

--
-- User Config "_avnadmin_monitor"
--

ALTER ROLE _avnadmin_monitor SET "extwlist.extensions" TO 'pg_stat_statements';
ALTER ROLE _avnadmin_monitor SET "pg_stat_monitor.pgsm_track" TO 'none';
ALTER ROLE _avnadmin_monitor SET "pg_stat_plans.track" TO 'none';
ALTER ROLE _avnadmin_monitor SET "pg_stat_statements.track" TO 'none';
ALTER ROLE _avnadmin_monitor SET "pgaudit.log" TO 'none';

--
-- User Config "postgres"
--

ALTER ROLE postgres SET avn_wal_sender_stop_timeout TO '10s';
ALTER ROLE postgres SET default_transaction_isolation TO 'read committed';
ALTER ROLE postgres SET default_transaction_read_only TO 'off';
ALTER ROLE postgres SET "pg_stat_monitor.pgsm_track" TO 'none';
ALTER ROLE postgres SET "pg_stat_plans.track" TO 'none';
ALTER ROLE postgres SET "pg_stat_statements.track" TO 'none';
ALTER ROLE postgres SET "pgaudit.log" TO 'none';
ALTER ROLE postgres SET "timescaledb.disable_load" TO 'true';


--
-- Role memberships
--

GRANT _avnadmin_auditing TO _avnadmin_managed WITH INHERIT TRUE GRANTED BY postgres;
GRANT _avnadmin_managed TO avnadmin_group WITH INHERIT TRUE GRANTED BY postgres;
GRANT avnadmin_group TO avnadmin WITH ADMIN OPTION, INHERIT TRUE GRANTED BY postgres;
GRANT pg_create_subscription TO _avnadmin_managed WITH INHERIT TRUE GRANTED BY postgres;
GRANT pg_monitor TO _avnadmin_monitor WITH INHERIT TRUE GRANTED BY postgres;
GRANT pg_read_all_stats TO _avnadmin_managed WITH INHERIT TRUE GRANTED BY postgres;
GRANT pg_signal_autovacuum_worker TO _avnadmin_managed WITH INHERIT TRUE GRANTED BY postgres;
GRANT pg_signal_backend TO _avnadmin_managed WITH INHERIT TRUE GRANTED BY postgres;
GRANT pg_stat_scan_tables TO _avnadmin_managed WITH INHERIT TRUE GRANTED BY postgres;
GRANT pg_use_reserved_connections TO _avnadmin_monitor WITH INHERIT TRUE GRANTED BY postgres;




\unrestrict rQuYRLQEm5eCtvgWfbUt4OUZljsbha4zmZn5uNqeG0Q7S7czStmWo2VXWvgZ5fc

--
-- PostgreSQL database cluster dump complete
--

