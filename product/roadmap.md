# Roadmap

Status: locked · 11 Jul 2026. Fresh build (no migration shims; old projects
migrate after v2 proves itself in dogfood). Each phase ends green on
`just verify`. Cut tickets from the bullets; the specs carry the detail.

## Phase 0 — Foundations (fresh build)

The v1 implementation was removed from the tree on 12 Jul (fresh space).
Survivors at the start of the rebuild were the path-safety kernel
(`core/paths.py`, `core/security.py` + adversarial tests), the tunnel scripts,
and a stub CLI. Everything below was built new from the specs — sessions never
exist in v2 code. Superseded implementation details are not retained.

- DB schema v2 + migration framework (spec-versioning §2.4:
  `PRAGMA user_version`, numbered files in `db/migrations/`, auto-applied
  at startup). The schema carries only: documents, FTS5 search, operations
  (pending-proposal fields per spec-mcp §5.2/§8: `operation_id`, `project`,
  `path`, `base_sha256`, TTL, state), snapshots, observations.
- Core config (env-synced per AGENTS.md rules), projects registry
  (`system/projects.yml` is the source of truth), document read/parse,
  snapshot machinery.
- Observation log: `correlation_id`, client metadata, `result_bytes`,
  `duration_ms`; metadata-only with redaction, per spec-mcp §8.

## Phase 1 — Layout, frontmatter v2, contract

- New workspace layout + folder-derived roles in core (paths, templates,
  `bootstrap_workspace.py` installs [contract/](contract/) and writes
  `system/meta.yml` with `format: 2`).
- Format gate in core: `FORMAT_UNSUPPORTED` on writes when the marker
  mismatches; `lattice migrate` CLI frame with empty migrator registry
  (spec-versioning §1).
- Frontmatter v2 (`status`, `edit_policy` + folder defaults); indexer
  columns; search filters.
- Search to FTS5: bm25 ranking, porter stemming, `snippet()`, sanitized
  match expressions (spec-versioning §2.3).
- `archive_document` / `unarchive_document` core flows (status + mirror
  move, snapshot-protected).
- Reconcile-on-read (stat check, drift → reindex + proposal staling +
  oplog) and watcher snapshot-on-detect debounce per spec-mcp §6.

## Phase 2 — MCP surface v2

- `get_context` (uncapped, payload telemetry, `format` echo).
- Re-scope all read/propose/apply tools to `project`; new error codes;
  policy echo on propose; fold canvas tool family.
- `initialize` instructions string.
- Integration test: every tool cold-callable per spec-mcp §10.1; adversarial
  out-of-band test §10.3.
- Agent configs: replace the AGENTS.md MCP Design Principles + tool
  taxonomy sections with v2; fix mcp-hardening skill scoping wording and
  error-code list; `just sync-agents`.

## Phase 3 — Dogfood (the validation, 00 D10)

- Create a fresh pilot project (garden v2 is the candidate: current phase
  docs recreated under the new layout by hand — not a migration, a
  restart).
- Paste bootstrap into a fresh ChatGPT project; live use for 1–2 weeks.
- Watch: contract compliance (ask-human honored? memory clean? policy notes
  respected?), `get_context` payload sizes (cap decision), and whether every
  mutation remains traceable.
- Exit criteria: a fresh chat handles a working-tempo session with nothing
  re-explained, and every mutation of a human-owned file traces to an
  explicit request.

## Phase 4 — Settle

- Decisions from dogfood data: get_context cap (or not), policy enforcement
  (only if agents proved unreliable — 00 principle 1's escape hatch).
- Migrate remaining old projects (plan written then, not now).
- Then the parked items, in whatever order usage demands: skills layer
  (index + triggers + due-ness — the attic drafts hold the candidate
  design), freshness metadata, sharing (per-project capability tokens,
  onboarding), headless agents, embeddings search (only if dogfood shows
  FTS5 retrieval falling short — spec-versioning §2.3).

Standing rule from spec-versioning §1.4, all phases: a breaking workspace
change is not done until its `N → N+1` migrator ships in the same change.
