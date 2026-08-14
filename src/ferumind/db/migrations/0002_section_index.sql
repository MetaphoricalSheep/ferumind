-- Section-level FTS5 mirror (RET-02). Identical to schema.sql so fresh and
-- migrated databases converge. Derived state only — durable history is untouched.
CREATE VIRTUAL TABLE IF NOT EXISTS section_index USING fts5(
    title,
    heading,
    body,
    project_key UNINDEXED,
    path UNINDEXED,
    section_id UNINDEXED,
    kind UNINDEXED,
    heading_text UNINDEXED,
    heading_path_json UNINDEXED,
    level UNINDEXED,
    start_line UNINDEXED,
    end_line UNINDEXED,
    content_sha256 UNINDEXED,
    size_bytes UNINDEXED,
    tokenize = 'porter unicode61'
);

-- Convergence, not cosmetics. Drift detection is stat-based
-- (core/reconcile.py compares mtime_ns/size_bytes), so an existing document
-- whose stat still matches its index row would never be re-read and would
-- keep zero section rows forever. Invalidating the stat signature while
-- leaving sha256 intact routes every document through reconcile's
-- touched-but-content-identical branch: index_file re-runs and writes section
-- rows, and because the hash is unchanged no out-of-band operation is logged
-- and no pending proposal is staled. Durable history is not modified.
-- -1 is unreachable for a real stat() result, so nothing can look converged
-- when it is not.
UPDATE documents SET mtime_ns = -1, size_bytes = -1;
