# Ferumind — Agent Instructions

## Mission

**Chats are disposable; Ferumind is where the continuity lives.** Ferumind is a
local-first, Markdown-backed workspace shared by a human and their agents:
they collaborate in working documents, agents keep auditable memory there, and
durable knowledge settles into a retrievable library. It provides a hardened
MCP server, project-scoped stores, safe file operations with snapshots and
rollback, and SQLite-backed indexing/search/operation logs.

## Product status

Ferumind is beta software. The locked product design lives in
`product/` ([00-what-is-ferumind.md](product/00-what-is-ferumind.md) and the
specs beside it); where existing code conflicts with
[product/spec-mcp.md](product/spec-mcp.md), **the spec wins**.

The current implementation was rebuilt from the specs in
[product/roadmap.md](product/roadmap.md). Superseded implementation details
are not part of the repository. The package is at 0.1.0 and has never shipped
a 1.0.

Three version numbers do not line up: the workspace `format` (1), the database
`schema` / `PRAGMA user_version` (3), and the package semver (0.1.0). Name the
axis whenever a number could be mistaken for another one; `v` is reserved for
git release tags. One relationship exists and runs one way: a format bump is
always breaking, so it always forces a package version bump — never the
reverse. See [product/spec-versioning.md](product/spec-versioning.md) §0.1.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                     Interface Layer                  │
│     MCP Server  │  CLI (Typer)  │  Local Dashboard  │
├─────────────────────────────────────────────────────┤
│                    Core Domain                       │
│  paths  │  config  │  registry  │  documents          │
│  context  │  policy  │  frontmatter  │  search         │
│  indexer  │  snapshots  │  operations  │  security    │
│  reconcile  │  format  │  locks  │  migrate           │
│  write_common  │  write_limits  │  patch_writes       │
│  document_writes  │  upload_writes                    │
│  lifecycle_writes  │  project_writes                  │
│  skills  │  compacts  │  lint  │  verify_index        │
│  files  │  file_reads  │  images  │  renditions       │
│  response_limits                                     │
│  observations  │  diagnostics  │  runtime events        │
└─────────────────────────────────────────────────────┘
```

The block names the domains, not every module; `src/ferumind/core/` is the
inventory.

The write domain is five modules, not one: guarded propose → apply in
`patch_writes`, creation and capture in `document_writes`, `library/` bytes
in `upload_writes`, archive/restore in `lifecycle_writes`, and project
creation in `project_writes`. `write_common` holds the guards they share and
`write_limits` the bounds; both are leaves. There is no `core/writes.py`.

`response_limits` is the read-side counterpart and also a leaf: what the
caller's transport will carry, spent as a `ResponseBudget` across the parts
of one result. It is a deliverability guard, not a cap — `get_context` stays
uncapped by the product contract, and nothing in that module truncates. A
response that provably cannot arrive is refused before it is emitted, because
the relay answers an oversized body with a 413 that kills the stdio child.

There is no workers layer. The filesystem watcher and backup worker were
removed: out-of-band edits are covered by reconcile-on-read, and no background
process is supervised.

There is no sessions module and no agents layer: the server is stateless per
call, and "knowledge agents" are procedures executed by whichever chat agent
is connected (product D13).

### Core principles

- Core logic belongs in `ferumind.core`.
- MCP and CLI must call core; they must not duplicate safety logic.
- Documents carry the intelligence; the server is a librarian: it protects,
  indexes, and assembles context — it never decides behavior.
- The workspace is live user data and must remain ignored by Git.
- Never write outside the configured workspace/project root.
- No symlink escape. No unsafe path joins.
- No hard delete for user knowledge content — archive/snapshot/restore flows.
- Every mutating operation is snapshot-protected and operation-logged.

## Repo Layout

```
ferumind/
  AGENTS.md              ← This file — committed agent instructions
  README.md
  pyproject.toml
  product/               ← Locked design: identity, specs, contract, roadmap
  .opencode/             ← Committed OpenCode config (source of truth)
  .githooks/             ← Git hooks (pre-commit, pre-push)
  scripts/               ← Bash and Python scripts
  src/ferumind/           ← Package root
    core/                ← Domain logic (typed, tested, no framework leakage)
    mcp/                 ← MCP server
    cli/                 ← Typer CLI
    dashboard/           ← Loopback API and packaged static operator UI
    db/                  ← SQLite schema and migrations
  tests/
    unit/                ← Unit tests
    integration/         ← Integration tests
    fixtures/            ← Test fixtures
  docs/                  ← Documentation
  workspace/             ← Live user data (Git-ignored)
```

### "Skill" means two different things — ask which one

The word is overloaded and the two mechanisms share nothing: not a location, not a
delivery path, not an audience.

| | **Repo skills** | **Ferumind skills** |
|---|---|---|
| Location | `.opencode/skills/<name>/SKILL.md` | `product/contract/skills/`, installed to `workspace/system/skills/` |
| Audience | Agents **building** Ferumind — you, in this repo | Agents **using** Ferumind — a chat client through MCP |
| Delivery | `scripts/sync_agent_configs.py` copies them to `.claude/`, `.cursor/`, `.codex/`, `.github/` at build time. No server involved | `get_context.skills` carries name + one-line trigger; `read_skill` fetches the body on demand |
| Examples | `mcp-hardening`, `test-fix`, `python-principal-engineer` | `distilling-durable-knowledge` |
| Status | Six exist and work | Triggers and index work; due-ness (cadence, `last_run`) is deliberately deferred |

Ferumind skills are the same kind of thing as workspace **rules** — behavior text
the server hands a connected agent — and differ only in being fetched on demand
rather than concatenated into every `get_context` call.

**If a request says "skill" and the intended one is not obvious from context, ask.**
Do not infer it from a grep: `.opencode/skills/` and `core/skills.py` both answer to
the word and mean opposite things. Adding a workspace procedure to
`.opencode/skills/` passes `just verify` and silently never reaches a chat agent.

When writing docs, tickets, or commit messages, say **"repo skill"** or **"Ferumind
skill"** whenever a bare "skill" could be read either way.

## Development Commands

| Command | Action |
|---------|--------|
| `just setup` | Install all dependencies |
| `just format` | Format code |
| `just lint` | Lint the repository with Ruff (not the workspace lint) |
| `just typecheck` | Type check |
| `just test` | Run tests |
| `just test-cov` | Tests with coverage |
| `just smoke` | Run the stdio smoke harness alone |
| `just retrieval-report` | Print the retrieval metric table |
| `just verify` | Full verification pipeline |
| `just sync-agents` | Sync agent configs |
| `just bootstrap` | Init workspace |
| `just dashboard` | Run the loopback-only operator dashboard |
| `just sync-basecoat <path>` | Refresh vendored Basecoat CSS from a local checkout |
| `just cli -- --help` | Run the CLI |
| `just project-list` | List projects across registry/folder/database |
| `just project-delete <key>` | Clean stale registry/DB state after its folder is gone |
| `just tunnel --init` | Initialize the tunnel profile |
| `just tunnel-bg` | Start the tunnel in the background |
| `just tunnel-stop` | Stop the background tunnel |
| `uv sync --all-extras --dev` | Install all dependencies |
| `uv run ruff format .` | Format code |
| `uv run ruff check .` | Lint code |
| `uv run pyright` | Type check |
| `uv run pytest` | Run tests |
| `uv run pytest --cov` | Tests with coverage |
| `scripts/verify.sh` | Full verification pipeline |
| `uv run python scripts/sync_agent_configs.py` | Sync agent configs |
| `uv run python scripts/bootstrap_workspace.py` | Init workspace |
| `uv run ferumind dashboard` | Run the loopback-only operator dashboard |
| `uv run ferumind` | Run the CLI |
| `uv run ferumind lint` | Report mechanical workspace findings; never edits Markdown |
| `uv run ferumind prune` | Report reclaimable derived state; deletes only with `--apply` |
| `just install-hooks` / `scripts/install-hooks.sh` | Install git hooks |

`just --list` is the complete recipe inventory; the table above is the subset
worth knowing by heart.

## Coding Standards

- Python 3.12-3.14. Ruff and Pyright target the **floor** (3.12), so
  newer-than-3.12 syntax or stdlib fails static checks before it can fail at
  runtime. Changing the supported range means changing `requires-python`, the
  classifiers, the CI matrix, and the README together — guards in
  `tests/unit/test_release_controls.py` fail otherwise. See
  [docs/python-support.md](docs/python-support.md).
- Strong typing everywhere
- `src/` layout
- Small cohesive modules, no god files
- No circular imports
- No hidden side effects at import time
- No untyped public APIs
- No broad unstructured dicts at module boundaries
- No hardcoded absolute paths
- No business logic in CLI/MCP layers
- Use `pathlib`, not `os.path`
- Use Pydantic v2 for config/schema validation at boundaries
- Use dataclasses/value objects for simple internal structures
- Clean error types, no broad `except Exception` without re-raise
- No hidden global state for app behavior
- `Any` requires written justification
- `# type: ignore` requires written justification

## Typing Standards

- Pyright in strict mode
- All public functions need explicit parameter and return types
- Use `Protocol` for duck-typed interfaces
- Use `TypedDict` for structured dicts
- Use `Literal` for constrained string values
- Use `NewType` for domain primitives (ProjectKey, RepoRoot, etc.)
- No `Any` without justification

## Testing Standards

- pytest for all tests
- Use `tmp_path` for filesystem tests
- Tests should cover both success and failure paths
- Path-security code requires adversarial tests
- No mocking core behavior unless necessary
- No tests that simply assert imports
- Tests must be deterministic
- No fake tests
- Coverage threshold: 80% global (target 90%+ during implementation)

## Security and Path Safety

- Workspace isolation: all user data in `workspace/`
- Project scoping: every operation scoped to a project
- Path validation: all paths through `core.paths.contained_path()`
- Symlink escape defense: `assert_no_symlink_escape()`
- No hard delete: prefer archive/snapshot/restore
- Operation log: every mutation recorded
- No writing through user-controlled paths without canonicalization

## Dashboard design system: Basecoat

Basecoat is Ferumind's dashboard design system. It owns the dashboard's visual
language; Ferumind continues to own architecture, security, privacy, data
boundaries, and product behavior.

- Use Basecoat semantic tokens for dashboard color and state. Dashboard components
  must not hard-code state hues or reach past semantic tokens into the primitive
  palette.
- Express state through Basecoat's `success`, `caution`, `danger`, and `neutral`
  tone system (plus the primary accent). Meaning must never depend on color alone.
- Reuse the vendored Basecoat components and patterns before inventing alternatives,
  including Panel, StatTile, StatusCard, StatusDot, LiveStatusDot, Chip, and Progress
  when semantically appropriate. Ferumind-specific tables, charts, and layouts may
  compose those primitives.
- A reusable visual pattern belongs upstream in Basecoat rather than as a generic
  clone inside Ferumind. Keep local CSS specific to the operator dashboard.
- Every dashboard change must meet WCAG 2.2 AA: semantic HTML, keyboard-operable
  controls, visible focus, sufficient contrast, reduced-motion support, and
  non-color status cues.
- The dashboard intentionally loads the theme from vendored package CSS and works
  offline. Do not add the Basecoat Tier 1 CDN runtime, Tailwind/Alpine CDN assets, a
  frontend build step, or Node as a Ferumind runtime requirement.
- The pinned Basecoat commit is recorded in
  `src/ferumind/dashboard/static/basecoat/REVISION`. Refresh the exact committed CSS
  with `just sync-basecoat /path/to/basecoat`; never hand-edit vendored CSS.
- Ferumind's Ruff, Pyright, Pytest, and repository verification pipeline remain
  authoritative. Basecoat's TypeScript tooling does not apply to this Python/static
  consumer.

## MCP Design Principles (product/spec-mcp.md is the full spec)

- **Stateless per call.** There are no sessions, no `session_id`, and no
  model-carried state except a short-lived patch `operation_id`. The server
  behaves identically if every call arrives on a fresh MCP connection.
- **Every project-scoped tool takes a required `project` argument.** It is an
  assertion validated against the registry — never an override. Missing →
  `PROJECT_REQUIRED`; unknown → `PROJECT_NOT_FOUND`. The project is the hard
  write boundary.
- `get_context` is the contract call: merged workspace + project rules, the
  spine, the document map, the Ferumind-skill index (name plus trigger, never
  a body), and the inbox count — uncapped, with payload-size telemetry.
- **Folder = role.** A document's role derives from its path (`spine.md`,
  `rules/`, `canvases/`, `memory/`, `library/`, `inbox/`, `archive/`). No
  `role:` frontmatter key. Behavioral frontmatter is `status`
  (active/gated/frozen/archived) and `edit_policy`
  (free/append/propose-first/ask-human) with folder defaults.
- **Propose → apply.** A `propose_*` result is not a saved edit: it carries
  `document_mutated=false`, `requires_apply=true`,
  `next_required_tool=apply_patch`. Only an `apply_patch` result with
  `document_mutated=true` means the file was saved. Proposals are bound to
  project + path + base hash, expire after 24 h (`PATCH_EXPIRED`), and are
  invalidated by out-of-band edits.
- **The server informs; agents honor.** Propose results echo the target's
  `edit_policy`/`status` with a `policy_note`; the server does not block on
  policy. The closed list of hard refusals: archived targets
  (`DOCUMENT_ARCHIVED`), protected frontmatter identity keys
  (`FRONTMATTER_PROTECTED`), out-of-project paths (`WORKSPACE_MISMATCH`).
- Out-of-band disk edits are first-class: reads reconcile (reindex, stale
  pending proposals, operation-log entry `source: out-of-band`). This is the
  only detection mechanism; there is no watcher. Hash guards make conflicting
  applies fail closed.
- Tool input schemas must be strict. No raw path writes; all paths resolve
  through the core path validator. All writes are snapshot-protected.
- Tool responses must be structured; errors must be machine-readable codes.
  No silent partial success. No hard delete (`archive_document` /
  `unarchive_document` instead).
- **Every tool advertises an `outputSchema`.** Tools declare
  `-> Annotated[CallToolResult, FerumindResult[Payload]]`; the SDK derives the
  schema from that metadata type and returns the hand-built result verbatim.
  Never pass `structured_output` — it short-circuits derivation and silently
  strips the schema. The schema covers success *and* failure arms, because the
  SDK validates `structuredContent` on `isError` results too. Result shape
  belongs in the schema; a tool's description says when to call it and what
  the result means, never a second copy of the field list. See the
  `mcp-tool-contracts` skill.
- Tool names are stable snake_case; the server namespace is Ferumind.
- The workspace format is versioned (`workspace/system/meta.yml`,
  `format: 1`; product/spec-versioning.md is the full spec). Format 1 is the
  floor — nothing precedes it. Writes against a mismatched format fail with
  `FORMAT_UNSUPPORTED`; migration is explicit (`ferumind migrate`), never
  implicit. The MCP surface itself is not wire-versioned: additive changes are
  free, while renames and removals are breaking and bump the package version.
  A format bump never lands without its migrator, fixtures, and tests in the
  same change — see Versioning and releases below. DB schema changes go
  through numbered migrations in `db/migrations/` (`PRAGMA user_version`) —
  never ad-hoc `ALTER` calls.

### Tool annotation taxonomy

Three categories ("read-only" means: does not mutate user Markdown; internal
observation-log metadata writes are allowed). Every registered tool appears
below, and `test_agents_md_classifies_every_registered_tool` fails when one
does not:

- **Read-only** (`readOnlyHint=true`, `idempotentHint=true`):
  `find_in_document`, `get_compact_instructions`, `get_context`,
  `get_document_map`, `list_compacts`, `list_files`, `list_pending_patches`,
  `list_projects`, `list_snapshots`, `list_tree`, `operation_log`,
  `read_compact`, `read_document`, `read_document_range`, `read_file`,
  `read_skill`, `read_snapshot`, `search_project`.
- **Staging** (`readOnlyHint=true`, `idempotentHint=false`):
  `append_upload_chunk`, `discard_patch`, `discard_upload`,
  `propose_exact_replace_patch`, `propose_frontmatter_patch`,
  `propose_insert_patch`, `propose_multi_edit_patch`, `propose_patch`,
  `propose_range_patch`, `propose_search_replace_patch`,
  `propose_section_patch`, `start_library_file_upload`. The `propose_*` family
  stages guarded diffs as pending operation records; the chunked-upload tools
  stage bytes against a pending `upload_id`. Neither writes user Markdown, and
  both expire after 24 h.
- **Content-mutating** (`readOnlyHint=false`, `idempotentHint=false`):
  `append_compact_chunk`, `apply_patch`, `archive_compact`,
  `archive_document`, `capture_note`, `create_compact_draft`,
  `create_document`, `create_project`, `finalize_compact`,
  `finalize_library_file_upload`, `rebuild_index`, `record_episode`,
  `restore_snapshot`, `resume_compact`, `unarchive_document`,
  `upload_library_file`, `upload_library_file_from_chatgpt`,
  `upload_library_files_from_chatgpt`.

## Non-Markdown files (spec-mcp §5.4)

A project holds arbitrary files alongside its Markdown. They are **not**
indexed, **not** searchable by content, and **never** part of `get_context`.
Two tiers serve them:

- `list_files` — generic discovery. Walks the project, returns
  project-relative paths with MIME type, size, a `ferumind://` `resource_uri`,
  and a `context_support` value (`image` / `text` / `resource_only`).
  Markdown, Ferumind upload sidecars, and `.ferumind/` are excluded by default.
- `read_file` — puts one file into model context: a bounded image rendition
  (JPEG/PNG/WebP), a bounded text slice, or metadata only. Always returns a
  `ResourceLink` to the untouched original.
- `resources/read` on that URI — the exact original bytes, never a rendition
  and never truncated.

`read_document` vs `read_file`: `read_document` is the managed Markdown
surface and returns frontmatter plus the `document_sha256` that hash-guarded
edits need. `read_file` is generic bytes and knows nothing about documents;
it will serve a `.md` as plain text but flags `recommended_tool:
read_document`.

There is **no prescribed folder** for files — `library/` is where uploads
land, not where files must live. A file's meaning never follows from its
folder, filename, or extension; the documents that reference it are the
source of truth. Paths are always project-relative, and the server-local
workspace path never appears in a URI, result, or error.

`read_file` does **not** extract PDF text, render PDF pages, parse Office
documents, or run OCR. A `resource_only` result means the model has not seen
the contents. Whether a linked resource gets attached to the conversation is
the client's decision, not the server's. Host-specific outbound file
mechanisms (OpenAI tool-file outputs, Anthropic file outputs) are explicitly
deferred.

## Observability

The SQLite observation log records recent MCP calls: tool, project,
client metadata when exposed by the transport, ok/error, duration, result
size, and argument keys. It records metadata only — never document content,
patch bodies, or argument values. Redaction replaces secrets/tokens with
`[redacted]`; unavailable client metadata remains null and is never guessed.
Exceptional and lifecycle events use the private metadata-only JSONL stream at
`.ferumind/logs/ferumind.jsonl`; exception messages, traceback text, locals,
arguments, and payloads are forbidden there. The separate operator dashboard
is read-only, loopback-only, never part of the MCP tunnel, and uses the same
core diagnostic/query layer as the CLI. Do not store operation ids or any tool
bookkeeping in Markdown documents.

## Derived index maintenance

Run `ferumind verify-index` from time to time, by judgement — not on every
commit, and not as part of `just verify`, the pre-commit hook, or CI. Default
mode is read-only: report the summary back to the owner. `--fix` rebuilds
derived index rows only and never writes Markdown; against the live workspace
it needs the owner's say-so.

## Storage retention

`ferumind prune` is the only thing that reclaims Ferumind's own accumulated
derived state — snapshot directories, applied-patch diffs, observation rows,
migration tarballs, blobs, the private runtime log. Nothing else deletes any
of it, and nothing runs it automatically: no scheduler, no startup hook, no
MCP tool. An agent must not be able to reclaim the user's history.

It reports and deletes nothing without `--apply`. Never pass `--apply` against
the live workspace without the owner's say-so, and stop the tunnel first
(`just tunnel-stop`) — a real run rewrites the database with `VACUUM`.

User knowledge is out of bounds, `archive/` most of all: it holds documents
the user chose to retire, not garbage. Same for `memory/`, `canvases/`,
`inbox/`, `rules/`, `compacts/`, `library/`, and `spine.md`. The derived
search index is `rebuild_index` and `verify-index --fix`'s to own, not
prune's.

The defaults in `core/retention.py` are local single-user ones. Hosted
retention policy is NET-021's and is not settled by them.

## Lookup-first editing

Agents must not patch large Markdown bodies when a narrower target is
available. Walk only as far as needed — never `read_document` first:

1. `search_project` — section hits carry `start_line`/`end_line`
2. `read_document_range` on a hit (skip `get_document_map` when the hit
   is enough)
3. `get_document_map` only when broader structure is genuinely useful; it
   can be large on long documents
4. `find_in_document` when you need an exact string inside a known document
5. `propose_exact_replace_patch` (preferred: exact multi-line old/new text),
   or `propose_multi_edit_patch` for several edits to one document
6. `propose_section_patch`, `propose_range_patch`,
   `propose_search_replace_patch`, or `propose_insert_patch` for positional
   edits
7. `apply_patch`

Frontmatter/metadata changes go through `propose_frontmatter_patch` (identity
keys `id`/`type`/`project`/`created` and the automatic `updated` are
protected).

Use `propose_patch mode=body` only when intentionally replacing the full
body. Use `mode=full` only for explicit full-file operations.

Positional (line/range/section) edits must be guarded by document and target
hashes. Content-anchored edits (exact replace, multi edit, frontmatter) are
guarded by the matched text itself; `expected_document_sha256` is optional
extra safety there. After `apply_patch`, chain its returned `document_sha256`
into the next edit's `expected_document_sha256` instead of re-reading.

Stale or superseded proposals should be withdrawn with `discard_patch`; new
documents are created with `create_document`, never through raw patches to
nonexistent paths.

## MCP Stdio / Tunnel Rules

- MCP stdio wrappers must not print non-MCP output to stdout.
- Tunnel launchers may log status to stderr/stdout (they are user-facing
  scripts, not MCP protocol participants).
- The actual MCP command given to tunnel-client should be a quiet executable
  wrapper, never a shell command string.
- `scripts/ferumind-mcp-stdio` is the canonical wrapper; it loads env files
  and `exec`s the server.
- The tunnel serves the workspace configured by `FERUMIND_WORKSPACE`. The MCP
  server does not authenticate callers, so the relay and its control-plane
  credentials are the only access control in front of that workspace. Treat
  the tunnel URL and `CONTROL_PLANE_*` values as secrets granting full read
  and write access.
- This is accepted for single-user development on a private deployment. The
  OAuth, owner-authorization, and deployment gate in product D11 must still
  ship before remote serving is supported for anyone but the workspace owner
  running it themselves.
- Never bypass the launcher with a direct `tunnel-client run`: the launcher
  validates the profile, records a PID bound to its process start time, and
  strips control-plane credentials from the MCP child's environment.
- Starting a tunnel is operator-initiated; the launcher refuses to start
  when `CI` is set.

## Versioning and releases

Ferumind is `0.MINOR.PATCH`. The leading zero means the versioned surfaces
still move. [docs/releases.md](docs/releases.md) is the full document; these
are the rules that bind you.

**The five versioned surfaces.** MCP tool surface (names, arguments, result
fields, error codes), workspace format, CLI commands and flags, config/env
keys, `ferumind://` resource URIs. **Nothing else is versioned** — the Python
import API is private and changes freely, and so does the SQLite schema.

**Classify every change** against those surfaces:

- **Breaking** — a format bump; a tool removed or renamed; a required argument
  added; an argument's meaning or type changed; a result field removed,
  renamed, or retyped; an error code removed or redefined; a CLI command or
  flag removed or renamed; a config key removed or a default that changes
  behavior; the Python floor rising.
- **Not breaking** — a new tool; a new *optional* argument; a new result
  field; a new error code for a new condition; a new CLI command or optional
  flag; a new config key with a safe default; a new folder or optional
  frontmatter key older code ignores; the Python ceiling rising; fixes,
  performance, docs, and refactors.

**Non-breaking is not the safe default.** When you cannot tell, say so in the
pull request. Do not pick the smaller label because it is less alarming.

**What you do:** add one line to `## [Unreleased]` in
[CHANGELOG.md](CHANGELOG.md) under `Breaking`, `Added`, `Changed`, or `Fixed`.
That is the whole obligation.

**What you never do:** edit `version` in `pyproject.toml`; create, move, or
delete a tag; push to `main`. Cutting a release is an owner act, and a pushed
tag is frozen forever.

**Format bumps** are always breaking and never land alone: the migrator, its
synthetic fixtures, and its tests belong in the same change. There is no
exception — the single-workspace one that predated publication is spent.

## Git Workflow

- `main` is trunk and is protected; never push to it directly. Branch → pull
  request → green `ci-gate` → merge.
- OpenCode is committed source of truth
- Other agent configs are generated by `scripts/sync_agent_configs.py`
- `.opencode/` is committed
- `.cursor/`, `.claude/`, `.codex/`, `.github/copilot-instructions.md` are
  generated and ignored
- Never commit live user knowledge data
- Never commit SQLite files
- Never commit generated agent folders for non-OpenCode agents
- Pre-commit hook runs: ruff format check, ruff lint, pyright, pytest
- Pre-push hook runs: `scripts/verify.sh`
- The remote default branch is the release source of truth

## Forbidden Actions

- Writing outside the configured workspace/project root
- Symlink escape
- Unsafe path joins — never use `str(path).startswith(str(root))` for
  containment checks; always use `is_under_root()` from `ferumind.core.paths`
- Hard delete for user knowledge content
- Reintroducing session state or `session_id` parameters (removed in the rebuild)
- Using `Any` without written justification
- Using `# type: ignore` without written justification
- Committing SQLite/DB files
- Committing live user knowledge data
- Committing generated agent configs
- Creating code with global mutable state
- Using `nano` in docs/scripts/instructions (use `vim`)
- Pushing to `main` directly, or editing `version` in `pyproject.toml`
- Creating, moving, or deleting a git tag
- Landing a workspace-format bump without its migrator, fixtures, and tests

## Env / Config Sync Rules

- `.env`, `.env.example`, and `src/ferumind/core/config.py` (or any Pydantic
  config module) must stay in sync.
- When adding a new key to one, add it to all three.
- Never overwrite existing values in `.env`. If a key already has a real
  value there, leave it alone.
- If the `.env` value looks sensitive (API key, secret, token, password),
  use a placeholder like `sk-xxx` or `your-{NAME}-here` in `.env.example`.
- When adding a new key to `.env.example`, use a safe placeholder value.
- When Pydantic config modules exist, the field names and defaults there are
  the source of truth for structure; `.env.example` is the source of truth
  for which variables are user-configurable.
- Tunnel control-plane keys (`CONTROL_PLANE_*`, `FERUMIND_TUNNEL_PROFILE`,
  `TUNNEL_CLIENT_BIN`) are launcher-only shell configuration. They must stay
  aligned between `.env`, `.env.example`, and the tunnel scripts, and must
  **not** be added to the application `Config` model or inherited by the MCP
  child. `scripts/ferumind-mcp-stdio` unsets that authority before `exec`.

## Required Verification Before Completion

1. Run `ruff format --check .` — no formatting issues
2. Run `ruff check .` — no lint errors
3. Run `pyright` — no type errors
4. Run `pytest --cov=src/ferumind` — all tests pass, coverage >= 80%
5. Review diff for TODOs, stubs, `Any`, `# type: ignore`
6. Check boundary violations (core vs interface)
7. Check security implications of any path/file operations

## Agent Delegation Policy

Primary planning and build agents must delegate verification repair to the
test-fixer subagent when implementation reaches the verification phase.

This applies even when the user did not explicitly ask for test fixing.

Required flow:

1. Plan or implement the requested work.
2. When the plan reaches verification, invoke the test-fixer subagent.
3. The test-fixer must use the test-fix skill.
4. The test-fixer must run the full verification pipeline, preferably via
   `just verify`.
5. The test-fixer may fix only safe verification failures.
6. The task is not complete until test-fixer returns TEST FIX COMPLETE.
7. If test-fixer returns ESCALATION REQUIRED, the primary agent must resume
   control and resolve the design, implementation, security, product,
   dependency, or human-decision issue before re-running verification.

Primary agents must not burn premium-model tokens on routine lint,
typecheck, unit-test, coverage, or smoke-test repair loops when test-fixer
is available.

Primary agents must not bypass test-fixer by declaring success from partial
checks.

Escalated failures must be treated as unfinished work, not as successful
completion.
