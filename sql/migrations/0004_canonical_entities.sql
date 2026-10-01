-- Internal identities are separate from source eligibility and frozen snapshots.
CREATE TABLE canonical_entities (
    entity_id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('TEAM','COMPETITION')),
    canonical_name TEXT NOT NULL CHECK (length(trim(canonical_name)) > 0),
    country_code TEXT NOT NULL CHECK (country_code ~ '^[A-Z]{2,3}$'),
    category TEXT NOT NULL CHECK (category ~ '^(MEN|WOMEN|MIXED):(SENIOR|U[0-9]{2}):(FIRST|B|II|RESERVE)$'),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance)='object' AND provenance <> '{}'::jsonb),
    external_ids JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(external_ids)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (entity_id,kind,country_code,category)
);

CREATE TABLE canonical_alias_versions (
    alias_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    entity_id UUID NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0 AND source <> '*'),
    alias TEXT NOT NULL CHECK (length(trim(alias)) > 0),
    normalized_alias TEXT NOT NULL CHECK (length(trim(normalized_alias)) > 0),
    country_code TEXT NOT NULL,
    category TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','RETRACTED')),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance)='object' AND provenance <> '{}'::jsonb),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (alias_id,version),
    FOREIGN KEY (entity_id,kind,country_code,category)
        REFERENCES canonical_entities(entity_id,kind,country_code,category),
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);
CREATE INDEX canonical_alias_lookup ON canonical_alias_versions
    (kind,source,normalized_alias,country_code,category,recorded_at);

CREATE FUNCTION canonical_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Canonical registry is append-only; append an alias revision';
END;
$$;
CREATE TRIGGER canonical_entities_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON canonical_entities FOR EACH STATEMENT EXECUTE FUNCTION canonical_reject_mutation();
CREATE TRIGGER canonical_aliases_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON canonical_alias_versions FOR EACH STATEMENT EXECUTE FUNCTION canonical_reject_mutation();

CREATE FUNCTION canonical_alias_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous canonical_alias_versions%ROWTYPE;
BEGIN
    -- Serialize revisions, including two concurrent first inserts.
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.alias_id,460244124));
    SELECT * INTO previous FROM canonical_alias_versions
        WHERE alias_id=NEW.alias_id ORDER BY version DESC LIMIT 1;
    IF FOUND THEN
        IF NEW.version <> previous.version+1 OR
           ROW(NEW.entity_id,NEW.kind,NEW.source,NEW.alias,NEW.normalized_alias,NEW.country_code,NEW.category)
           IS DISTINCT FROM
           ROW(previous.entity_id,previous.kind,previous.source,previous.alias,previous.normalized_alias,previous.country_code,previous.category)
        THEN RAISE EXCEPTION 'Alias revision sequence or identity changed'; END IF;
    ELSIF NEW.version <> 1 THEN
        RAISE EXCEPTION 'Alias revisions must start at version 1';
    END IF;
    -- Knowledge time is server-owned; it cannot be backdated by an import.
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER canonical_alias_version_insert BEFORE INSERT ON canonical_alias_versions
    FOR EACH ROW EXECUTE FUNCTION canonical_alias_revision_guard();
