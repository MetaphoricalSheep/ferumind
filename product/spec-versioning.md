# Spec: versioning & the SQLite layer

Status: locked · 12 Jul 2026 · schema-current amendment 9 Aug 2026 · decisions taken with the user (four forks:
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
| The package itself | semver in `pyproject.toml` | n/a — source checkout only today |

### 0.1 Naming the axes

Each axis gets one name, and the names are not interchangeable:

- **`format N`** — the workspace layout. Lives in `meta.yml`, enforced by
  `core/format.py`.
- **`schema N`**, or `PRAGMA user_version` — the SQLite schema.
- **semver** (`0.1.0`) — the package release, in `pyproject.toml`.
- **`v`** is reserved for git release tags (`v0.2.0`) and appears nowhere
  else.

**These numbers move independently and will never line up.** The workspace is
at format 3 while the database is at schema 3 and the package is at 0.1.0;
those two matching is a coincidence of history, not a relationship — they
moved for unrelated reasons and will diverge again. Write the axis name
whenever a number could be read as belonging to another one: "format 3", not
a bare version label. A label attached to no axis rots silently, because
nothing checks it — so a guard in `tests/unit/test_release_controls.py` does.

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
format: 3
created: "2026-07-12"
```

- `format` is a positive integer. The current layout
  ([spec-mcp.md](spec-mcp.md) §2) is format `3`; format 1 and format 2 layouts
  both genuinely existed before it. Format 3 differs from format 2 in one
  respect: `description` is a required frontmatter key on every managed
  document (spec-mcp §3).
- A missing or unreadable marker means the format is **unknown**, not old.
  Ferumind never substitutes a number for it: an unmarked directory is an
  uninitialized workspace, and the remedy is to bootstrap it, not to migrate
  it. There is no starting format to migrate from.
- **Whole-workspace granularity** (decided): one `format` governs every
  project. The server supports exactly one format; there is no mixed-format
  or per-project version handling, and no importer between formats — a
  workspace moves forward through `ferumind migrate` or not at all.
- `bootstrap_workspace.py` writes the marker when initializing a workspace;
  `ferumind migrate` rewrites it as the final step of a successful migration.

### 1.2 Server behavior on mismatch

Checked once per process against the workspace being served (cheap stat +
parse, cached; re-checked if the file's mtime changes — meta.yml is subject
to out-of-band edits like everything else):

| Condition | Reads | Writes (propose/apply/create/archive/restore) |
|---|---|---|
| `format` == supported | normal | normal |
| `format` < supported | entrypoints allowed; each document must satisfy the current parser | refused with `FORMAT_UNSUPPORTED` |
| marker missing or unreadable | entrypoints allowed; each document must satisfy the current parser | refused with `FORMAT_UNSUPPORTED` |
| `format` > supported | refused with `FORMAT_UNSUPPORTED` | refused with `FORMAT_UNSUPPORTED` |

- `FORMAT_UNSUPPORTED` is a machine-readable error code (spec-mcp §7). Its
  message states the found format, the supported format, and a remedy that
  can actually run: `ferumind migrate` for an old workspace, upgrading the
  server for a new one, and bootstrapping for an unmarked directory — never
  `migrate` for the last, which has no starting format to migrate from.
- Older markers do not close read entrypoints, so a semantically prepared
  workspace remains inspectable while the human executes migration. This is
  not a second legacy parser: an individual document that violates the current
  managed-document contract still fails closed. Format 2 → 3 deliberately
  prepared every description before the marker bump; the owner-authorized
  one-workspace exception does not promise arbitrary unprepared format-2 trees
  are readable by format-3 code. A newer-than-supported workspace is refused
  entirely — an old server writing new-format files would corrupt silently.
- `get_context` echoes the workspace marker in its payload block (`3` for a
  current workspace, an older integer for an older marker, and `null` when
  the marker is unreadable), when context assembly itself satisfies the
  current document parser. It never substitutes the build's
  supported format, so the workspace version remains truthful in transcripts
  and the observation log.

### 1.3 `ferumind migrate` (explicit, human-triggered)

Decided: migration of user Markdown is never implicit. The CLI command:

```
ferumind migrate [--dry-run]
```

1. Reads `meta.yml`, resolves the chain of registered format migrators
   (`N → N+1`, applied in sequence). No path → clear error.
2. Runs each step's registered **preflight**, if it has one: prerequisite
   validation that a format can require before it is safe to transform
   anything. This runs after re-planning under the workspace lock and
   **before** the backup exists, so a workspace that simply is not ready yet
   is refused with `MIGRATION_PREREQUISITE_UNMET` having changed nothing and
   written no recovery marker. That distinction is the point: a failed
   *transformation* means the tree may be half-converted and a human must
   restore a backup; a failed *prerequisite* means the operator has more
   preparation to do. Preflights must not write.
3. Creates a full workspace tarball backup **and** a global snapshot before
   touching anything, then durably arms
   `.ferumind/MIGRATION_RECOVERY_REQUIRED.json` before transformation begins.
   This is a replay guard as well as a caught-exception report: process death
   or power loss cannot leave a partially transformed tree eligible to run the
   same in-place migrator again.
4. Runs each migrator (pure functions over the workspace tree: move
   folders, rewrite frontmatter, install/refresh `system/` contract files).
   `--dry-run` prints the plan without writing.
5. Runs a full reindex and refuses on any document error, writes and commits
   the migration audit rows as one SQLite transaction, advances the replay
   guard to `audit_committed`, and only then bumps `meta.yml` as the final
   publication step. The format marker is the declaration that the cutover
   completed and the gate that re-enables writes, so it remains on the old
   format if derived-state reconstruction or audit persistence fails. The
   replay guard is cleared only after publication. If power fails between
   those last two writes, `audit_committed` plus the target format proves the
   migration completed and prevents replay despite the stale guard file. If
   format-marker replacement becomes visible but its directory fsync cannot
   be proven, the replay guard deliberately remains: a reboot may restore the
   old marker, but it still cannot make the in-place migration replayable.

If a migrator fails after transformation begins, Ferumind updates the already
armed `.ferumind/MIGRATION_RECOVERY_REQUIRED.json` with the private backup path
and safe failure metadata, then refuses to rerun migration. The operator must
restore that backup before a retry; an in-place migrator is never replayed over
a partially transformed workspace. Migration audit rows are committed as one
SQLite transaction.

**Ferumind shipped the frame, not a migration**: the marker, the
`FORMAT_UNSUPPORTED` gate, the `migrate` command with an empty migrator
registry, and its tests (fake `1→2` migrator in tests only). The first real
migrator was written when the first format bump needed one — the `2 → 3`
bump that made `description` required — and the standing rule below is what
guaranteed migration was proven in the same work unit.

That migrator was **local one-shot tooling and was never committed**. It was
deleted once the owner's workspace was migrated, returning the registry to
empty before the permanent format-3 change landed. Ferumind has one user and
one workspace, so the spent `2 → 3` migrator can never run against another
supported workspace; tracking it, even briefly, would create repository
history for a code path that was never part of the product. Completion
evidence records the verified format-2 backup and the one-shot execution, not
a migrator commit SHA. Restoring that backup therefore also means deliberately
recreating and re-auditing the migration step; that recovery tradeoff is an
explicit owner decision. The **frame** stays: registry, preflights,
`plan_migration`, `create_backup_tarball`, `run_migration`, the recovery
marker, and the CLI command are product machinery for the next bump, and they
are not what gets deleted.

### 1.4 The format-bump rule (why there is never another rebuild)

> **A breaking workspace change is not done until migration is proven in the
> same migration work unit.** Amendments accumulate in `product/` as they
> emerge (the existing pattern); when a change breaks layout, frontmatter, or
> contract semantics, the work that lands it must include the format bump,
> tested `N → N+1` migration, updated contract files, successful live cutover,
> and a `just sync-agents` pass. A migrator normally ships with the breaking
> change. The explicit single-owner/single-workspace exception used for format
> 2 → 3 may instead be audited, tested, executed, evidenced, and deleted as
> untracked one-shot tooling before landing. Additive changes (new optional
> frontmatter key, new tool, new folder that old servers simply ignore) do
> **not** bump the format.

## 2. The SQLite layer

Verdict from the 12 Jul audit: SQLite is the right engine (local-first,
zero-ops, WAL, stdlib) — kept. The audit found the layer under-finished
rather than over-engineered: 4 of 9 tables had no readers, search was a
`LIKE` scan with a hardcoded score, and schema changes were ad-hoc `ALTER`s.
The design that came out of it keeps **less DB, better used**.

### 2.1 Two roles, stated explicitly

The schema header must declare which tables are which:

- **Derived index** — rebuildable from Markdown at any time via
  `rebuild_index`: `documents`, `search_index`, `section_index` (FTS5
  mirrors). A migration may simply drop and rebuild these.
- **Durable system history** — *not* rebuildable, migrated with care:
  `operations` (audit + pending proposals), `snapshots` (registry),
  `mcp_call_observations`. This is system data, not knowledge data: it lives
  in SQLite, never in Markdown, never in git.

The project registry's source of truth is `system/projects.yml` (existing
mechanism, spec-mcp §1); the DB carries no copy.

### 2.2 Tables the schema does not carry (decided)

The audit found five tables that earned no place in the baseline. They are
**absent** from `schema.sql` rather than created and dropped: the baseline is
a single clean statement of what the system uses, and no migration exists to
unwind them.

| Table | Why it is not there |
|---|---|
| `sessions` | D8 — no sessions |
| `canvases` | Folder = role; no canvas entity (D4) |
| `projects` | Would be a write-only mirror of `projects.yml`; zero readers |
| `document_blocks` | Was write-only and half-implemented (headings only, empty hashes), with a duplicate-row bug |
| `document_links` | Was write-only, with a duplicate-row bug |

The blocks/links duplication bug (UUID primary keys + `INSERT OR REPLACE`
never replacing) has no home to recur in. Backlinks / block indexes return as
real *read* features if and when something needs them, with a natural unique
key and a reader on day one.

A future migration must not reintroduce any of these to restore symmetry with
a history the tree no longer contains.

### 2.3 Search: FTS5 (decided)

Two FTS5 mirrors, both maintained by the indexer (delete + insert keyed by
`project_key`/`path`):

- **`search_index`** — one row per document (`title`, `body`). Still maintained
  by the indexer for derived-state parity / recovery; **not** the active
  `search_project` surface after RET-03.
- **`section_index`** — one row per derived Markdown section
  (`title`, `heading`, `body`, plus UNINDEXED section metadata). Sections come
  from `core.document_map.derive_sections` — the same parser
  `get_document_map` and the patch resolver use. This is the active
  `search_project` surface. `bm25()` weights are title=1.0, heading=2.0,
  body=1.0; the FTS5 `snippet()` window is 24 tokens (RET-03 harness
  measurement on the post-RET-05 corpus).

Shared properties:

- Tokenizer: `porter unicode61` (stemming + unicode folding).
- Ranking: `bm25()`; `SearchResult.score` carries the real rank.
- Snippets: FTS5 `snippet()`.
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

Numbered migrations, never ad-hoc `ALTER`s:

- `PRAGMA user_version` tracks the schema version (starts at the number of
  shipped migrations, so a baseline with no migrations sits at 0).
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

The current database is at schema 3. Migration `0001` adds the non-unique
correlation-ID index used by operator diagnostics; it changes no observation
content and preserves historical opaque identifiers. Migration `0002` adds
the `section_index` FTS5 mirror and invalidates document stat signatures so
reconcile repopulates section rows without inventing out-of-band audit
entries. Migration `0003` adds `documents.description` and invalidates stat
signatures for the same reason: drift detection is stat-based, so a document
whose stat still matched its row would keep an empty column until the next
full rebuild.

### 2.5 What stays

Per-call connections with WAL and `busy_timeout` (right for stdio
concurrency), the operations/snapshots/observations design as amended by
spec-mcp §8, and the no-DB-files-in-git rule.

## 3. Phase impact

- **Phase 0**: DB migration framework (§2.4) landed first with an empty
  `db/migrations/` and a baseline that already omitted the tables in §2.2.
  The first subsequent migration is schema 1's correlation-ID index.
- **Phase 1**: `meta.yml` written by `bootstrap_workspace.py`; FTS5 schema +
  indexer (§2.3); `FORMAT_UNSUPPORTED` gate in core; `ferumind migrate`
  frame (§1.3).
- **Phase 2**: `get_context` format echo; `FORMAT_UNSUPPORTED` in the MCP
  error surface (spec-mcp §7).
- **Phase 4 / Open**: embeddings design session (only if dogfood shows FTS5
  retrieval falling short); the first real workspace migrator arrives with
  the first format-breaking change (§1.4).
