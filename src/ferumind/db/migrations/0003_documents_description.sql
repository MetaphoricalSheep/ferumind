-- documents.description (FMT-01). The declaration and its position in the
-- resulting table match schema.sql so fresh and migrated databases converge.
-- Derived state only — durable history is untouched.
ALTER TABLE documents ADD COLUMN description TEXT NOT NULL DEFAULT '';

-- Convergence, for the same reason RET-02's migration needed it. Drift
-- detection is stat-based (core/reconcile.py compares mtime_ns/size_bytes), so
-- a document whose stat still matches its index row would never be re-read and
-- would keep an empty description until the next full rebuild. Invalidating the
-- stat signature while leaving sha256 intact routes every document through
-- reconcile's touched-but-content-identical branch: index_file re-runs and
-- fills the column, and because the hash is unchanged no out-of-band operation
-- is logged and no pending proposal is staled. Durable history is not modified.
-- -1 is unreachable for a real stat() result, so nothing can look converged
-- when it is not.
UPDATE documents SET mtime_ns = -1, size_bytes = -1;
