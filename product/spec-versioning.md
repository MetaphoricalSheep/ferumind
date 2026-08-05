# Spec: versioning & the SQLite layer (v2)

Status: locked · 12 Jul 2026 · decisions taken with the user (four forks:
whole-workspace granularity, explicit migrate, FTS5 now + embeddings parked,
drop the dead tables). Companion to [spec-mcp.md](spec-mcp.md); where the two
overlap, the amendments in spec-mcp §4/§7/§8 are the same decisions restated.

## 0. The principle: version the workspace, not the API

Three things change shape over Ferumind's life, and they get three different
treatments:

| What | Versioned how | Migrated how |
|---|---|---|
| Workspace format (folders, frontmatter contract, `system/` files) | `format:` in `workspace/system/meta.yml` | Explicit `ferumind migrate`, human-triggered, snapshot-protected |
| DB schema | `PRAGMA user_version` + numbered migrations | Automatic at startup (system state, largely rebuildable) |
| MCP tool surface / contract text | **Not wire-versioned** | Additive within a format version; breaking changes ride a format bump |

The MCP surface needs no version negotiation because chats are disposable:
there are no long-lived clients to break. The connector discovers tools at
connect time, and the behavior contract is *data* — `get_context` delivers
the current rules to every fresh chat, so an upgraded server re-teaches
agents automatically. The only durable client-side artifact is the bootstrap
prompt, which stays minimal and version-agnostic (D9). Tool names are stable;
parameters may be added (optional) within a format version; renames and
removals only happen together with a format bump.

## 1. Workspace format version

### 1.1 The marker

`workspace/system/meta.yml`, human-readable, part of the Markdown-side truth:

```yaml
# Ferumind workspace metadata. Managed by Ferumind; do not edit by hand.
format: 2
created: "2026-07-12"
```

- `format` is a positive integer. The v2 layout ([spec-mcp.md](spec-mcp.md)
  §2) is format `2`; v1 workspaces predate the marker (absence ⇒ format 1).
- **Whole-workspace granularity** (decided): one `format` governs every
  project. The server supports exactly one format; there is no mixed-format
  or per-project version handling. Old v1 content enters a v2 workspace via
  a one-way importer (Phase 5, D14), never by running two formats side by
  side.
- `bootstrap_workspace.py` writes the marker when initializing a workspace;
  `ferumind migrate` rewrites it as the final step of a successful migration.

### 1.2 Server behavior on mismatch

Checked once per process against the workspace being served (cheap stat +
parse, cached; re-checked if the file's mtime changes — meta.yml is subject
to out-of-band edits like everything else):

| Condition | Reads | Writes (propose/apply/create/archive/restore) |
|---|---|---|
| `format` == supported | normal | normal |
| `format` < supported (or marker missing) | allowed | refused with `FORMAT_UNSUPPORTED` |
| `format` > supported | refused with `FORMAT_UNSUPPORTED` | refused with `FORMAT_UNSUPPORTED` |

- `FORMAT_UNSUPPORTED` is a machine-readable error code (spec-mcp §7). Its
  message states the found format, the supported format, and the remedy
  (`run: ferumind migrate` for old workspaces; upgrade the server for new
  ones).
- Reads stay open on an old workspace deliberately: the library must remain
  consultable while the human decides when to migrate. A newer-than-supported
  workspace is refused entirely — an old server writing new-format files
  would corrupt silently.
- `get_context` echoes `"format": 2` in its payload block so the workspace
  version is visible in transcripts and the observation log.

### 1.3 `ferumind migrate` (explicit, human-triggered)

Decided: migration of user Markdown is never implicit. The CLI command:

```
ferumind migrate [--dry-run]
```

1. Reads `meta.yml`, resolves the chain of registered format migrators
   (`N → N+1`, applied in sequence). No path → clear error.
2. Creates a full workspace tarball backup **and** a global snapshot before
   touching anything.
3. Runs each migrator (pure functions over the workspace tree: move
   folders, rewrite frontmatter, install/refresh `system/` contract files).
   `--dry-run` prints the plan without writing.
4. Bumps `meta.yml`, triggers a full reindex, writes an operation-log entry
   (`operation_type: migrate`).

If a migrator fails after transformation begins, Ferumind writes
`.ferumind/MIGRATION_RECOVERY_REQUIRED.json` with the private backup path and
refuses to rerun migration. The operator must restore that backup before a
retry; an in-place migrator is never replayed over a partially transformed
workspace. Migration audit rows are committed as one SQLite transaction.

**The v2 build ships the frame, not a migration**: the marker, the
`FORMAT_UNSUPPORTED` gate, the `migrate` command with an empty migrator
registry, and its tests (fake `1→2` migrator in tests only). The first real
migrator is written when v3 first needs one — and the standing rule below
guarantees that happens.

### 1.4 The v3 rule (why there is never another rebuild)

> **A breaking workspace change is not done until its migrator ships in the
> same change.** Amendments accumulate in `product/` as they emerge (the
> existing pattern); when a change breaks layout, frontmatter, or contract
> semantics, the PR that lands it must include: the format bump, the
> `N → N+1` migrator with tests, updated contract files, and a
> `just sync-agents` pass. Additive changes (new optional frontmatter key,
> new tool, new folder that old servers simply ignore) do **not** bump the
> format.

## 2. The SQLite layer

Verdict from the 12 Jul audit: SQLite is the right engine (local-first,
zero-ops, WAL, stdlib) — kept. The v1 layer was under-finished, not
over-engineered: 4 of 9 tables had no readers, search was a `LIKE` scan with
a hardcoded score, and schema changes were ad-hoc `ALTER`s. v2 keeps **less
DB, better used**.

### 2.1 Two roles, stated explicitly

The schema header must declare which tables are which:

- **Derived index** — rebuildable from Markdown at any time via
  `rebuild_index`: `documents`, `search_index` (and the FTS mirror). A
  migration may simply drop and rebuild these.
- **Durable system history** — *not* rebuildable, migrated with care:
  `operations` (audit + pending proposals), `snapshots` (registry),
  `mcp_call_observations`. This is system data, not knowledge data: it lives
  in SQLite, never in Markdown, never in git.

The project registry's source of truth is `system/projects.yml` (existing
mechanism, spec-mcp §1); the DB carries no copy.

### 2.2 Dropped tables (decided)

Removed in the Phase 0 migration, alongside the de-sessioning drops:

| Table | Why |
|---|---|
| `sessions` | D8 — no sessions |
| `canvases` | Folder = role; no canvas entity (D4) |
| `projects` | Write-only mirror of `projects.yml`; zero readers |
| `document_blocks` | Write-only, half-implemented (headings only, empty hashes), duplicate-row bug |
| `document_links` | Write-only, duplicate-row bug |

The blocks/links duplication bug (UUID primary keys + `INSERT OR REPLACE`
never replacing — the same bug fixed for `canvases` in v1) dies with the
tables. Backlinks / block indexes return as real *read* features if and when
something needs them, with a natural unique key and a reader on day one.

### 2.3 Search: FTS5 (decided)

`search_index` becomes a contentless-delete FTS5 table (or external-content
over a base table — implementer's choice, FTS5 either way):

- Tokenizer: `porter unicode61` (stemming + unicode folding).
- Ranking: `bm25()`; `SearchResult.score` carries the real rank (today it is
  hardcoded `0.0`).
- Snippets: FTS5 `snippet()` replaces the hand-rolled extractor.
- Multi-term and prefix queries work; the raw user query is sanitized into
  an FTS match expression (quote terms; no raw injection of FTS syntax —
  `VALIDATION_ERROR` on unbalanced quotes rather than a 500).
- Filters: `project` scoping always; `folder` and `status` columns for
  role-filtered search (spec-mcp §5 `search_project`).
- No new dependencies: FTS5 is compiled into Python's bundled SQLite.

**Embeddings are parked, not rejected** (decided: "FTS5 now + plan
embeddings"). Semantic search needs an embedding source (API cost, or a
local model) and a chunking policy — a post-dogfood design session *if*
FTS5-quality retrieval proves insufficient in real use. Listed in 00 §Open.

### 2.4 DB migration framework

Replaces the ad-hoc `_add_column` calls in `db/database.py`:

- `PRAGMA user_version` tracks the schema version (starts at the number of
  shipped migrations).
- Migrations are numbered files in `src/ferumind/db/migrations/`
  (`0001_<slug>.sql`, or `.py` when data transformation is needed), applied
  in order inside a transaction at startup by `Database.init_schema()`;
  `user_version` bumps per migration.
- Fresh databases: apply `schema.sql` (kept current), then set
  `user_version` to the latest — no replay of history.
- Derived-index migrations may be implemented as drop + set-reindex-flag.
- DB migrations are automatic (unlike workspace migrations): the DB is
  system state; the durable-history tables get normal transactional
  migrations, and everything else is rebuildable.

### 2.5 What stays

Per-call connections with WAL and `busy_timeout` (right for stdio + watcher
concurrency), the operations/snapshots/observations design as amended by
spec-mcp §8, and the no-DB-files-in-git rule.

## 3. Phase impact

- **Phase 0**: DB migration framework (§2.4) lands first; the de-session
  migration and the table drops (§2.2) are its first real migrations.
- **Phase 1**: `meta.yml` written by `bootstrap_workspace.py`; FTS5 schema +
  indexer (§2.3); `FORMAT_UNSUPPORTED` gate in core; `ferumind migrate`
  frame (§1.3).
- **Phase 2**: `get_context` format echo; `FORMAT_UNSUPPORTED` in the MCP
  error surface (spec-mcp §7).
- **Phase 5 / Open**: embeddings design session (only if dogfood shows FTS5
  retrieval falling short); first real workspace migrator arrives with the
  first v3-breaking change (§1.4).
