-- OWNER STEP (issue #187, WP-009): least-privilege grants for signer migrations 0002/0003.
-- NOT a migration: the runner never loads this directory. Run it only on the SIGNER database, by the owner,
-- after the WP-009 prerequisites, with psql variables naming the two existing NOLOGIN group roles:
--   psql "$SIGNER_DB" -v ON_ERROR_STOP=1 -v mcp_role=<mcp role> -v signer_role=<signer role> -f <this file>
-- No DELETE, TRUNCATE or UPDATE for the MCP role anywhere; it must never own these tables.
\set ON_ERROR_STOP on
BEGIN;
-- 0002: the MCP queues requests (idempotent insert) and reads events; the signer reads requests, appends events.
GRANT SELECT, INSERT ON signer_order_requests TO :"mcp_role";
GRANT SELECT ON signer_order_request_events TO :"mcp_role";
GRANT SELECT ON signer_order_requests TO :"signer_role";
GRANT SELECT, INSERT ON signer_order_request_events TO :"signer_role";
-- queue_seq: the insert trigger calls nextval as the inserting role (MCP); the signer takes release watermarks.
GRANT USAGE ON SEQUENCE signer_order_requests_queue_seq TO :"mcp_role";
GRANT USAGE ON SEQUENCE signer_order_requests_queue_seq TO :"signer_role";
-- 0003: the signer publishes its status (upsert of its own row); the MCP only reads it.
GRANT SELECT ON signer_status TO :"mcp_role";
GRANT SELECT, INSERT, UPDATE ON signer_status TO :"signer_role";
COMMIT;
