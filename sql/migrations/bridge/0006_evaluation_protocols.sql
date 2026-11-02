-- New evidence only. No historical tickets/snapshots/results are modified.
CREATE TABLE evaluation_protocols (
    protocol_id TEXT PRIMARY KEY CHECK (length(trim(protocol_id)) > 0),
    contract_version TEXT NOT NULL DEFAULT 'frozen-ticket-v1' CHECK (contract_version='frozen-ticket-v1'),
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL CHECK (window_end > window_start),
    policy_json TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    sealed_at_utc TIMESTAMPTZ NOT NULL
);

CREATE FUNCTION seal_evaluation_protocol() RETURNS trigger LANGUAGE plpgsql AS $seal$
DECLARE
    p JSONB;
    scoring JSONB;
    field TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Evaluation protocols are append-only';
    END IF;
    IF NEW.sealed_at_utc IS NOT NULL OR NEW.policy_sha256 IS NOT NULL THEN
        RAISE EXCEPTION 'Evaluation seal and hash are server-owned';
    END IF;
    NEW.sealed_at_utc := clock_timestamp();
    IF NEW.window_start <= NEW.sealed_at_utc THEN
        RAISE EXCEPTION 'Evaluation must be registered before its future window';
    END IF;
    p := NEW.policy_json::jsonb;
    IF jsonb_typeof(p) <> 'object' OR NOT p ?& ARRAY['window_start','window_end','strategy',
        'min_fixtures','min_days','min_coverage','min_log_loss_gain','min_roi',
        'result_grace_days','commission','score_policy'] OR
        (SELECT count(*) FROM jsonb_object_keys(p)) <> 11 THEN
        RAISE EXCEPTION 'Complete evaluation policy required';
    END IF;
    IF jsonb_typeof(p->'window_start') <> 'string' OR jsonb_typeof(p->'window_end') <> 'string' OR
        jsonb_typeof(p->'strategy') <> 'string' OR
        (p->>'window_start') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' OR
        (p->>'window_end') !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' OR
        (p->>'window_start')::timestamptz <> NEW.window_start OR
        (p->>'window_end')::timestamptz <> NEW.window_end OR
        p->>'strategy' !~ '^S[1-9]$' THEN
        RAISE EXCEPTION 'Evaluation window or strategy mismatch';
    END IF;
    FOREACH field IN ARRAY ARRAY['min_fixtures','min_days','min_coverage','min_log_loss_gain',
                                 'min_roi','result_grace_days','commission'] LOOP
        IF jsonb_typeof(p->field) <> 'number' THEN
            RAISE EXCEPTION 'Numeric evaluation threshold required: %',field;
        END IF;
    END LOOP;
    FOREACH field IN ARRAY ARRAY['min_fixtures','min_days','result_grace_days'] LOOP
        IF (p->>field)::numeric <> trunc((p->>field)::numeric) OR (p->>field)::numeric > 1000000 THEN
            RAISE EXCEPTION 'Integer evaluation count required: %',field;
        END IF;
    END LOOP;
    IF (p->>'min_fixtures')::numeric < 2 OR (p->>'min_days')::numeric < 2 OR
        (p->>'min_days')::numeric > (p->>'min_fixtures')::numeric OR
        (p->>'result_grace_days')::numeric < 0 OR
        (p->>'min_coverage')::numeric <= 0 OR (p->>'min_coverage')::numeric > 1 OR
        (p->>'min_log_loss_gain')::numeric <= 0 OR
        (p->>'min_roi')::numeric <= 0 OR (p->>'min_roi')::numeric >= 1 OR
        (p->>'commission')::numeric < 0 OR (p->>'commission')::numeric >= 1 THEN
        RAISE EXCEPTION 'Invalid evaluation thresholds';
    END IF;
    scoring := p->'score_policy';
    IF jsonb_typeof(scoring) <> 'object' OR NOT scoring ?& ARRAY['epsilon','bin_edges','min_bin_count',
        'bootstrap_replicates','bootstrap_seed','confidence_level'] OR
        (SELECT count(*) FROM jsonb_object_keys(scoring)) <> 6 THEN
        RAISE EXCEPTION 'Complete scoring policy required';
    END IF;
    FOREACH field IN ARRAY ARRAY['epsilon','min_bin_count','bootstrap_replicates',
                                 'bootstrap_seed','confidence_level'] LOOP
        IF jsonb_typeof(scoring->field) <> 'number' THEN
            RAISE EXCEPTION 'Numeric scoring parameter required: %',field;
        END IF;
    END LOOP;
    FOREACH field IN ARRAY ARRAY['min_bin_count','bootstrap_replicates','bootstrap_seed'] LOOP
        IF (scoring->>field)::numeric <> trunc((scoring->>field)::numeric) THEN
            RAISE EXCEPTION 'Integer scoring parameter required: %',field;
        END IF;
    END LOOP;
    IF (scoring->>'epsilon')::numeric <= 0 OR (scoring->>'epsilon')::numeric >= 0.5 OR
        (scoring->>'min_bin_count')::numeric < 1 OR (scoring->>'min_bin_count')::numeric > 1000000 OR
        (scoring->>'bootstrap_replicates')::numeric < 1000 OR (scoring->>'bootstrap_replicates')::numeric > 10000 OR
        (scoring->>'confidence_level')::numeric < 0.95 OR (scoring->>'confidence_level')::numeric >= 1 OR
        jsonb_typeof(scoring->'bin_edges') <> 'array' THEN
        RAISE EXCEPTION 'Invalid scoring or uncertainty policy';
    END IF;
    IF jsonb_array_length(scoring->'bin_edges') < 2 OR
        scoring->'bin_edges'->0 <> '0'::jsonb OR scoring->'bin_edges'->-1 <> '1'::jsonb OR
        EXISTS (SELECT 1 FROM jsonb_array_elements(scoring->'bin_edges') WITH ORDINALITY AS edges(value,pos)
            WHERE jsonb_typeof(value) <> 'number' OR
                (pos>1 AND value::numeric <= (scoring->'bin_edges'->(pos::int-2))::numeric)) THEN
        RAISE EXCEPTION 'Invalid fixed calibration bins';
    END IF;
    NEW.policy_sha256 := encode(sha256(convert_to(NEW.policy_json,'UTF8')),'hex');
    RETURN NEW;
END;
$seal$;

CREATE TRIGGER evaluation_protocols_seal BEFORE INSERT OR UPDATE OR DELETE ON evaluation_protocols
    FOR EACH ROW EXECUTE FUNCTION seal_evaluation_protocol();
CREATE TRIGGER evaluation_protocols_no_truncate BEFORE TRUNCATE ON evaluation_protocols
    FOR EACH STATEMENT EXECUTE FUNCTION seal_evaluation_protocol();
