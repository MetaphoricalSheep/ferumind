-- Ferumind SQLite schema (database schema 3 baseline).
--
-- Two table roles (product/spec-versioning.md §2.1):
--
--   Derived index — rebuildable from Markdown at any time via rebuild_index:
--     documents, search_index, section_index (FTS5 mirrors).
--
--   Durable system history — not rebuildable, migrated with care:
--     operations (audit + pending proposals), snapshots (registry),
--     mcp_call_observations.
--
-- The project registry's source of truth is workspace/system/projects.yml;
-- the database carries no copy. Markdown files remain the source of truth
-- for all knowledge content. Schema changes ship as numbered migrations in
-- db/migrations/ tracked by PRAGMA user_version — never ad-hoc ALTERs.

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

-- ── Derived index ────────────────────────────────────────────────────────────

-- description is the format-3 navigation summary served beside size_bytes.
-- It is empty only for Markdown without managed-document identity frontmatter.
-- Keep the column immediately before the table constraint: SQLite places an
-- ALTER TABLE ADD COLUMN there, so fresh and migrated table DDL converge.
CREATE TABLE IF NOT EXISTS documents (
    project_key TEXT NOT NULL,
    path TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    folder TEXT NOT NULL,
    status TEXT NOT NULL,
    edit_policy TEXT NOT NULL,
    frontmatter_json TEXT NOT NULL DEFAULT '{}',
    sha256 TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_key, path)
);

CREATE INDEX IF NOT EXISTS idx_documents_project_folder
    ON documents(project_key, folder);

-- FTS5 mirror of documents, maintained by the indexer (delete + insert keyed
-- by project_key/path). porter unicode61 gives stemming + unicode folding;
-- ranking uses bm25(), snippets use snippet() (spec-versioning §2.3).
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    title,
    body,
    project_key UNINDEXED,
    path UNINDEXED,
    tokenize = 'porter unicode61'
);

-- Section-level FTS5 mirror: one row per derived Markdown section, refreshed
-- with its document (delete + insert keyed by project_key/path). Sections come
-- from core.document_map.derive_sections -- the same parser get_document_map
-- and the patch resolver use -- so an indexed line range is the range an edit
-- resolves against. title/heading/body are separate indexed columns so bm25()
-- can weight a heading match above the same term buried in prose; RET-03
-- chooses the weights. Everything else is UNINDEXED metadata for the reader.
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

-- ── Durable system history ───────────────────────────────────────────────────

-- Audit log for every mutation plus pending patch proposals. Proposal rows
-- (operation_type propose_*) are keyed by an unguessable operation id and
-- bound to project + path + base_sha256 with a 24 h TTL (spec-mcp §5.2/§8).
-- state: pending | applied | discarded | stale | expired | failed
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    tool_name TEXT NULL,
    target_path TEXT NULL,
    source TEXT NOT NULL DEFAULT 'agent',
    request_json TEXT NOT NULL DEFAULT '{}',
    base_sha256 TEXT NULL,
    after_sha256 TEXT NULL,
    diff_text TEXT NULL,
    snapshot_id TEXT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_project ON operations(project_key);
CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state);
CREATE INDEX IF NOT EXISTS idx_operations_target
    ON operations(project_key, target_path);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    target_path TEXT NULL,
    snapshot_dir TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_project ON snapshots(project_key);

-- Metadata-only observation log for MCP calls (spec-mcp §8): tool name,
-- correlation id, client info, timing and payload sizes — never document
-- content, patch bodies, or argument values.
CREATE TABLE IF NOT EXISTS mcp_call_observations (
    id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    project_key TEXT NULL,
    created_at TEXT NOT NULL,
    ok INTEGER NULL,
    error_code TEXT NULL,
    transport TEXT NULL,
    server_boot_id TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    client_name TEXT NULL,
    client_version TEXT NULL,
    protocol_version TEXT NULL,
    duration_ms REAL NULL,
    result_bytes INTEGER NULL,
    context_metrics_json TEXT NOT NULL DEFAULT '{}',
    argument_keys_json TEXT NOT NULL DEFAULT '[]',
    redaction_notes_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_mcp_call_observations_created_at
    ON mcp_call_observations(created_at);

CREATE INDEX IF NOT EXISTS idx_mcp_call_observations_tool_name
    ON mcp_call_observations(tool_name);

CREATE INDEX IF NOT EXISTS idx_mcp_call_observations_correlation_id
    ON mcp_call_observations(correlation_id);
