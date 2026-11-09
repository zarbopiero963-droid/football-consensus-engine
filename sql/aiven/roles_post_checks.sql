-- FCB-WP-009: after finalize, inside the roles transaction. On the FIRST run (:fcb_first_run set by
-- sql/aiven/roles_apply_guard.sql) the object counts are asserted with the restore's own file,
-- which reads public.schema_migrations: safe there, because fcb_migrator was created in this
-- same uncommitted transaction and nobody else can have planted code in its objects. On a
-- RE-RUN the administrative identity reads no bridge table (see roles_apply_guard.sql); the
-- counts are then checked by the workflow in read-only sessions, and the model (which pins
-- every routine, trigger and relation kind) by assert_bridge_roles.sql.
\if :fcb_first_run
\ir assert_bridge_counts.sql
\endif
