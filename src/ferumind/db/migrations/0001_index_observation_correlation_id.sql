-- Correlation ids are opaque incident-investigation keys. They are not
-- unique: preserving a non-unique index keeps historical/corrupt duplicate
-- rows inspectable instead of making a migration fail closed on them.
CREATE INDEX IF NOT EXISTS idx_mcp_call_observations_correlation_id
    ON mcp_call_observations(correlation_id);
