-- Existing rows deliberately remain NULL: no retroactive certification.
ALTER TABLE source_pilot_runs ADD COLUMN sealed_at_utc TIMESTAMPTZ;
ALTER TABLE source_pilot_predictions ADD COLUMN sealed_at_utc TIMESTAMPTZ;

CREATE FUNCTION seal_pilot_row() RETURNS trigger LANGUAGE plpgsql AS $seal$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Pilot evidence is append-only; do not rewrite historical slots';
    END IF;
    IF NEW.sealed_at_utc IS NOT NULL THEN
        RAISE EXCEPTION 'Pilot sealing time is server-owned';
    END IF;
    NEW.sealed_at_utc := clock_timestamp();
    RETURN NEW;
END;
$seal$;

CREATE TRIGGER source_pilot_runs_seal
    BEFORE INSERT OR UPDATE OR DELETE ON source_pilot_runs
    FOR EACH ROW EXECUTE FUNCTION seal_pilot_row();
CREATE TRIGGER source_pilot_predictions_seal
    BEFORE INSERT OR UPDATE OR DELETE ON source_pilot_predictions
    FOR EACH ROW EXECUTE FUNCTION seal_pilot_row();
CREATE TRIGGER source_pilot_runs_no_truncate
    BEFORE TRUNCATE ON source_pilot_runs
    FOR EACH STATEMENT EXECUTE FUNCTION seal_pilot_row();
CREATE TRIGGER source_pilot_predictions_no_truncate
    BEFORE TRUNCATE ON source_pilot_predictions
    FOR EACH STATEMENT EXECUTE FUNCTION seal_pilot_row();
