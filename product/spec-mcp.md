# Spec: MCP Server v2

Status: locked for build · 11 Jul 2026 · implements
[00-what-is-lattice.md](00-what-is-lattice.md) D3, D7, D8, D9, D11, D12, D13.
Written to be buildable by a coding agent without further product decisions.

This is a **fresh surface**. No compatibility shims with the session-based v1
tools; the old workspace layout is not served. Migration of old projects is a
separate later effort (00 §Open).

## 0. Transport, protocol, statelessness

- Server framework: current FastMCP/MCP SDK stack, local stdio transport,
  launched by `scripts/lattice-mcp-stdio`. Direct network transports fail
  closed. The tunnel integration may serve a live workspace for an owner
  running it themselves; the OAuth, owner-authorization, and deployment gate
  in D11 still governs supported remote serving beyond that case.
- **Stateless per call.** No tool reads or writes any server-side
  conversation state. Every scoped call carries `project`; patch continuity
  is carried by `operation_id` + content hashes. The server must behave
  identically if every call arrives on a fresh MCP session (ChatGPT already
  does this).
- Target MCP protocol semantics: 2026-07-28 revision (no reliance on
  `Mcp-Session-Id`, no init-time state). Remain compatible with the current
  SDK; upgrading the SDK is a roadmap item, not a blocker. **Build-start
  task:** record the SDK and protocol version actually in use in the ticket.
- `initialize` `instructions` field returns the condensed bootstrap (§9).

## 1. Scoping

One endpoint. Every project-scoped tool takes:

```
project: str   # project key, e.g. "garden"; validated against projects.yml
```

- Missing/empty → `PROJECT_REQUIRED`. Unknown → `PROJECT_NOT_FOUND`.
- `project` is an assertion, never an override: it selects which project
  root all paths resolve under, through the existing `core.paths` validators
  (`contained_path`, `assert_no_symlink_escape`, `is_under_root`).
- Workspace-level tools (`list_projects`, `create_project`) take no
  `project`.
- There are **no session tools**. Deleted outright: `start_session`,
  `get_session`, `clear_session`, `set_project`, `clear_project`,
  `set_canvas`, `clear_canvas`, `validate_project`, `debug_list_sessions`,
  and every `session_id` parameter. Deleted error codes: `SESSION_REQUIRED`,
  `SESSION_MISMATCH`, `SESSION_NOT_FOUND`, `WORKSPACE_LOCKED`.

## 2. Workspace layout (fresh, D2)

```
workspace/
  AGENTS.md                    # generated pointer; content from system/rules/
  compacts/                    # workspace-level chat handoff compacts
  system/
    rules/                     # workspace-level rules, *.md, lexical order
    prompts/bootstrap.md       # canonical bootstrap prompt
    templates/                 # project seed templates (see contract/)
    schemas/
    projects.yml               # project registry (existing mechanism)
  projects/<key>/
    spine.md                   # the entry page; fixed name, project root
    rules/                     # project rules, *.md
    canvases/                  # working documents; nests freely
    memory/                    # agent memory; nests freely
    library/                   # reference; nests freely
    inbox/                     # capture buffer
    archive/                   # mirror of the folder a doc came from
```

- **Folder = role.** A document's role is derived from its first path
  segment (`spine.md` at root = spine). No `role:` frontmatter key exists.
  The indexer stores the derived folder as a column for search filtering.
- Role folders nest arbitrarily below the first segment.
- Documents may not be created outside these top-level entries (`create_document`
  validates the first path segment; `UNKNOWN_FOLDER` otherwise).
- `compacts/` is outside project scope. It stores workspace-level handoff
  compacts for free-floating chats and is served only by the compact tools;
  compacts are not project documents, are not indexed as project documents,
  and never appear in `get_context`.

## 3. Frontmatter v2 (D3)

```yaml
id: doc_…                # existing stable id, server-generated
type: document           # constant; replaces type: canvas
project: <key>
title: <str>
status: active           # active | gated | frozen | archived; default active
edit_policy: free        # optional: free | append | propose-first | ask-human
created: <iso>           # server-set
updated: <iso>           # server-maintained, protected
```

- **Protected keys** (existing mechanism, unchanged): `id`, `type`,
  `project`, `created`; `updated` is automatic. `propose_frontmatter_patch`
  refuses them (`FRONTMATTER_PROTECTED`).
- `edit_policy` **defaults by folder** when the key is absent:
  `canvases/` → `free`, `memory/` → `free`, `inbox/` → `free`,
  `library/` → `propose-first`, `rules/` → `ask-human`,
  `spine.md` → `propose-first`. A log canvas is an ordinary canvas whose
  author sets `edit_policy: append` explicitly.
- No `gate:`, `cadence:`, `last_run:`, `as_of:` keys in v1. Gate conditions
  are prose in the document/spine. (Skills machinery is phase 2.)
- Indexer: add `status`, `edit_policy` (resolved, i.e. explicit-or-default),
  and `folder` columns; `search_project` and `get_context` read them.

## 4. `get_context` (D9)

The contract call; first call of every chat.

Request: `{ project }`

Response:

```json
{
  "project": { "key": "garden", "title": "Garden", "status": "active" },
  "rules": {
    "content_markdown": "<workspace rules then project rules, concatenated, each file prefixed by an H2 header naming its source path>",
    "sources": ["system/rules/00-contract.md", "projects/garden/rules/training.md"]
  },
  "spine": {
    "path": "spine.md",
    "content_markdown": "…full spine…",
    "document_sha256": "…"
  },
  "documents": [
    { "path": "canvases/phase-1.md", "title": "Phase 1", "folder": "canvases",
      "status": "active", "edit_policy": "free", "updated": "2026-07-10T…" }
  ],
  "inbox_count": 2,
  "payload": { "format": 2, "rules_bytes": 4120, "spine_bytes": 6210, "documents_count": 14 }
}
```

Rules:

- `rules.content_markdown` is concatenation with source headers — **no
  semantic merging** (D7). Order: `system/rules/*` lexically, then
  `projects/<key>/rules/*` lexically.
- `spine` is `null` with `"spine_missing": true` if `spine.md` doesn't exist.
- `documents` excludes `status: archived` and everything under `archive/`
  and `inbox/` (inbox is a count). Includes `rules/` and `memory/` files —
  the map is complete otherwise.
- **Uncapped in v1, observed** (decision 11 Jul): no truncation anywhere.
  The server records `rules_bytes`, `spine_bytes`, `documents_count`, and
  total result bytes in the observation log on every `get_context` call, and
  the same numbers are echoed in the `payload` field so they're visible in
  transcripts. A cap is a later decision made from this data.

## 5. Tool inventory

Common result envelope: existing structured-result pattern stays (machine-
readable `error.code` + `error.message` on failure; no silent partial
success). Existing parameter names are **kept verbatim** (`path`,
`old_string`, `new_string`, `occurrence`, `expected_match_count`,
`expected_document_sha256`, …) with `session_id` replaced by `project`.

### 5.1 Read-only (`readOnlyHint=true, idempotentHint=true`)

| Tool | Params | Returns | Notes |
|---|---|---|---|
| `get_context` | `project` | §4 | |
| `get_compact_instructions` | — | compacting procedure | explicit `/compact` or Lattice compact trigger only |
| `read_document` | `project, path` | content, frontmatter, `document_sha256` | serves every folder incl. rules/memory/archive |
| `read_document_range` | `project, path, start_line, end_line` | lines + hashes | unchanged |
| `get_document_map` | `project, path` | section map + hashes | unchanged |
| `find_in_document` | `project, path, query, …` | matches | unchanged |
| `search_project` | `project, query, folder?, status?, include_archived=false, limit?` | hits with `folder`/`status` per row | new filters |
| `list_tree` | `project, folder?` | tree listing | unchanged mechanics; **Markdown only** — non-Markdown files are `list_files` (§5.4) |
| `list_files` | `project, path_prefix?, query?, mime_type?, extension?, include_markdown=false, include_sidecars=false, limit=100, cursor?` | file entries with path, MIME, size, `resource_uri`, `context_support` | §5.4; generic non-Markdown discovery, walks the project |
| `read_file` | `project, path, max_image_edge=1024, image_quality=78, text_offset=0, max_text_chars=50000` | typed MCP content: image rendition / bounded text / resource link | §5.4; returns real `ImageContent` + `ResourceLink`, never the original bytes inline |
| `list_pending_patches` | `project` | pending proposals (id, path, age, expires_at) | project-scoped now |
| `operation_log` | `project, path?, limit?` | recent operations | includes `source: out-of-band` entries |
| `list_snapshots` | `project, path?, limit?` | snapshot list | absorbs `list_canvas_snapshots` |
| `read_snapshot` | `project, snapshot_id` | snapshot metadata, bounded text content, omission flags, and diff | rename of `read_canvas_snapshot` |
| `list_projects` | — | projects with key/title/status | workspace-level |
| `read_compact` | `token` | compact frontmatter/body/hash status | workspace-level |
| `list_compacts` | `state?, project?, limit?` | compact metadata only | workspace-level; validates project filter if supplied |

Dropped (folded into the document tools): `read_canvas`, `list_canvases`,
`read_canvas_diff`, `read_canvas_operation`, `list_canvas_operations`,
`project_overview` (superseded by `get_context`).

### 5.2 Proposal tools (`readOnlyHint=true, idempotentHint=false`)

Unchanged mechanics and parameters, minus `session_id`, plus `project`:

`propose_exact_replace_patch` (preferred), `propose_multi_edit_patch`,
`propose_section_patch`, `propose_range_patch`,
`propose_search_replace_patch`, `propose_insert_patch`,
`propose_frontmatter_patch`, `propose_patch` (coarse fallback,
`mode=body|full`), `discard_patch(project, operation_id)`.

Proposal semantics:

- Result carries `operation_id`, `document_mutated: false`,
  `requires_apply: true`, `next_required_tool: "apply_patch"`, and a
  **policy echo**: `{ "edit_policy": "...", "status": "...", "policy_note": "..." }`.
  `policy_note` strings (exact copy, one per policy):
  - `append` → "This document is append-only: only additions at the anchor
    or end are appropriate."
  - `propose-first` → "This document expects curation: tell the user what
    will change before applying."
  - `ask-human` → "This file is human-owned: apply only if the user
    explicitly requested this change in this conversation."
  - `frozen` (status) → "Structure is frozen: additions only, no
    restructuring."
  The server **does not block** on policy (00 principle 1).
- Hard refusals (the closed list): target `status: archived` or path under
  `archive/` → `DOCUMENT_ARCHIVED`; protected frontmatter key →
  `FRONTMATTER_PROTECTED`; path outside the project → `WORKSPACE_MISMATCH`.
- `operation_id`: unguessable (≥128-bit random), persisted as a pending
  operation bound to `project`, `path`, and the base `document_sha256`.
  TTL **24 h**; expired apply → `PATCH_EXPIRED`.
- Pending-operation retention is bounded per project: at most 1,000 rows and
  64 MiB total serialized request/diff content. Terminal transitions
  (`applied`, `discarded`, `stale`, `expired`, `failed`) scrub the staged
  replacement content and diff while retaining audit metadata.
- Positional patches (section/range) remain hash-guarded as today;
  content-anchored patches remain guarded by matched text with optional
  `expected_document_sha256`.

### 5.3 Content-mutating (`readOnlyHint=false, idempotentHint=false`)

| Tool | Params | Behavior |
|---|---|---|
| `apply_patch` | `project, operation_id` | revalidates binding + guards and the stored replacement's size/hash/Markdown structure; snapshot-before-write; file compensation plus one SQLite commit for snapshot/oplog/proposal terminal state; returns new `document_sha256` for chaining |
| `create_document` | `project, folder_path, title, content, status?, edit_policy?` | absorbs `create_canvas`; `folder_path` must start with a role folder (else `UNKNOWN_FOLDER`); generates frontmatter; snapshot; oplog |
| `capture_note` | `project, text, title?` | writes into `inbox/` (existing mechanics) |
| `archive_document` | `project, path` | sets `status: archived` **and** moves to `archive/<original-path>`; snapshot; oplog; returns `archived_path`. Refuses `spine.md` (`CANNOT_ARCHIVE_SPINE`) |
| `unarchive_document` | `project, archived_path` | reverse: move back to mirror origin, `status: active`; snapshot; oplog. Collision at origin → `PATH_EXISTS` |
| `restore_snapshot` | `project, snapshot_id` | unchanged |
| `create_project` | `key, title` | workspace-level; registers in `projects.yml`; seeds `spine.md` + folder skeleton from `system/templates/` (see [contract/](contract/)) |
| `rebuild_index` | `project?` | unchanged |
| `create_compact_draft` | `project?, sources?, tags?` | workspace-level; creates `workspace/compacts/compact_<four-word-token>.md` |
| `append_compact_chunk` | `token, chunk_markdown, sources?` | appends an agent-produced chunk summary to a draft compact |
| `finalize_compact` | `token, handoff_prompt, final_markdown, sources?, tags?` | requires the body to start with the handoff prompt block; stores body hash |
| `resume_compact` | `token, auto_archive_on_resume=false` | verifies integrity, increments `resume_count`, returns handoff prompt + body |
| `archive_compact` | `token` | sets compact `state: archived`; no hard delete |

Current surface: **17 read + 12 propose/discard + 17 mutate = 46 tools**,
zero session tools.

### 5.3a Library file upload (experimental, added post-lock)

Added on the `feature/library-file-upload` branch, after the rest of this
spec was locked — not part of the original 11 Jul design. Kept here rather
than silently diverging code from spec (00 principle: spec wins).

| Tool | Params | Behavior |
|---|---|---|
| `upload_library_file` | `project, filename, content_base64, folder_path="library", mime_type?, metadata?` | Writes a binary file under `library/` plus a `<stem>.json` metadata sidecar (extension replaced, not appended to); snapshot; oplog |

Rationale and constraints:

- **Why base64, not a native binary type.** MCP tool arguments are JSON;
  JSON has no binary type. Base64 in a string field is the standard MCP
  idiom for inline binary payloads (the same approach MCP's own `image`/
  embedded-resource content blocks use for their `blob` field) — there is no
  alternative that avoids the ~33% wire-size inflation.
- **Always under `library/`.** `folder_path` works exactly like
  `create_document`'s (nests freely below the role folder) but its first
  segment must be `library`; anything else → `UNKNOWN_FOLDER`.
- **Direct write, not propose/apply.** Binary content has no meaningful
  text diff, so this tool is content-mutating (`document_mutated: true`)
  immediately — there is no proposal step for uploads.
- **Extension denylist, not allowlist.** Unlike every other write path
  (Markdown-only, `ALLOWED_EXTENSIONS = {".md"}`), uploads block a fixed set
  of script/executable extensions and allow everything else. Blocked →
  `UNSUPPORTED_FILE_TYPE`.
- **Size cap.** Decoded payload capped at `MAX_CHUNK_BYTES` (256 KB;
  `core/writes.py`), not `MAX_UPLOAD_BYTES` — this whole call has to be
  deliverable in one tool call, and real MCP-client tool-call size
  ceilings (ChatGPT's connector included) turned out to sit far below what
  the wire format alone allows. 10 MB and then 100 MB were both tried
  first and both proved undeliverable in practice; §5.3b's chunked path
  exists precisely so a file doesn't have to fit in one call at all. Over
  the cap → `FILE_TOO_LARGE`, pointing at the chunked tools instead.
- **Fails closed on collision.** If a file already exists at the target
  path (or its metadata sidecar does), the call refuses with
  `DOCUMENT_EXISTS` — no silent overwrite, no auto-versioning.
- **Metadata sidecar is agent-authored, with a few protected fields.** The
  server always stamps `original_filename`, `uploaded_at`,
  `uploaded_by_tool`, `sha256`, `size_bytes`, `mime_type` into
  `<stem>.json` (the uploaded file's own extension is replaced, not
  appended to — `photo.jpg` → `photo.json`, not `photo.jpg-metadata.json`;
  a same-stem collision across different extensions, e.g. `photo.jpg` and
  `photo.png`, therefore collides on the sidecar and fails closed even
  though the content files themselves wouldn't). A `.json` upload is the
  necessary exception: its sidecar is `<filename>.metadata.json`, so metadata
  cannot overwrite the uploaded content itself. Any other keys the caller
  passes in `metadata` are kept verbatim and are the agent's to shape
  (tags, description, source, etc.) — the server does not validate their
  schema.
- **Known gap: not indexed.** The indexer, `list_tree`, and
  `search_project` are Markdown-only today (`index_project` walks
  `*.md`; `parse_document` assumes frontmatter). Uploaded files and their
  metadata sidecars exist on disk and in the operation/snapshot log, but
  are invisible to those three read tools until the indexer is extended to
  cover them. `read_document`/`get_document_map`/`find_in_document` also
  reject non-`.md` paths with `DOCUMENT_NOT_FOUND` (existing
  `_read_project_file` suffix check) — expected, not a new restriction.
- **Binary snapshot reads are explicit.** Snapshot metadata and the binary
  addition note remain readable, but a side that cannot be decoded as UTF-8
  (or exceeds the Markdown mutation limit) is returned as `null` with its
  corresponding `*_content_omitted: true` flag. This prevents a binary
  upload from turning `read_snapshot` into an internal decoding failure or
  an unbounded response. Before/after sides are streamed and verified against
  the stored byte count and SHA-256 before any content is returned, with a
  64 MiB stored-file verification ceiling. Text sides and `diff.patch` are
  each capped at 5 MiB in the response; an oversized diff is empty with
  `diff_omitted: true`. Missing, undeclared, or corrupted sides fail closed
  with `SNAPSHOT_NOT_FOUND`.
- New error codes: `UNSUPPORTED_FILE_TYPE`, `FILE_TOO_LARGE` (§7 addendum
  below).

### 5.3b Chunked library file upload (experimental, added post-lock)

Added after §5.3a shipped and was exercised for real: a ChatGPT-uploaded
photo set landed with one file silently corrupted (a few bytes missing
inside a small ~3.6 KB JPEG) despite the tool call reporting success. The
`sha256` recorded in that file's own metadata sidecar matched the corrupted
bytes exactly — proving the write path persisted precisely what it was
given — so the corruption was already present in the `content_base64`
string by the time it reached the tool call. The likely cause: the calling
agent's connector had the model reproduce a long, repetitive base64 string
as generated text rather than a deterministic encode, and long repetitive
runs are exactly what LLM text generation is prone to garbling. Notably the
corrupted file was *small* — this is not purely a large-file problem.

Two independent mitigations, both added here:

| Tool | Params | Behavior |
|---|---|---|
| `start_library_file_upload` | `project, filename, total_size, total_chunks, folder_path="library", mime_type?, metadata?, expected_sha256?` | Declares a pending upload; returns an unguessable `upload_id` (reuses the patch-proposal `operation_id` generator and 24h TTL/pending state machine, `operation_type="upload_session"`) |
| `append_upload_chunk` | `project, upload_id, chunk_index, chunk_base64` | Stages one chunk (capped at `MAX_CHUNK_BYTES`, 256 KB decoded — shared with `upload_library_file`'s single-call cap) to `projects/<key>/.lattice/uploads/<upload_id>/chunks/`; idempotent per index — a resend overwrites, never duplicates |
| `finalize_library_file_upload` | `project, upload_id` | Verifies every chunk 0..total_chunks-1 present (`UPLOAD_INCOMPLETE` otherwise), assembles them, checks the assembled size against the declared `total_size`, verifies `expected_sha256` if supplied (`CONTENT_HASH_MISMATCH` otherwise), then runs the *same* write path as `upload_library_file` (§5.3a) — identical result shape, extension denylist, fail-closed collision; snapshot row, upload oplog, and terminal session state commit atomically |
| `discard_upload` | `project, upload_id` | Abandons a pending session and deletes its staged chunks; nothing was ever written to `library/` |

Rationale:

- **Smaller pieces, not a different transport.** MCP tool arguments are
  still JSON (§5.3a's base64 rationale is unchanged) — chunking doesn't add
  a raw-binary channel, it just bounds how much base64 text needs to be
  correct in any single generated string. A client whose connector encodes
  bytes deterministically gains nothing from chunking; a client where the
  model is retyping base64 text gains a smaller blast radius per call, and
  either way gets a hash check it didn't have before.
- **`expected_sha256` is the real fix for the actual bug found.** It
  doesn't stop a model from garbling base64, but it turns silent
  corruption into a loud, retryable `CONTENT_HASH_MISMATCH` instead of a
  file that reports success and is broken. Available on the chunked path
  only for now (`start_library_file_upload`'s `expected_sha256`); the
  one-shot `upload_library_file` (§5.3a) doesn't take one — worth adding
  there too if silent corruption shows up on small, unchunked uploads.
- **Sessions reuse the proposal state machine, not new DB schema.** An
  upload session is an `operations` row (`operation_type="upload_session"`)
  with the same `pending → applied|discarded|expired|failed` states,
  `expires_at` TTL, and project-scoped lookup (`OPERATION_NOT_FOUND`,
  `PATCH_PROJECT_MISMATCH` on a cross-project `upload_id`) as a patch
  proposal — no migration needed. Chunk bytes themselves never enter the
  DB; they live only as files under `.lattice/uploads/`.
- **Abandoned staging is bounded and opportunistically swept.** A project
  may reserve at most 32 pending upload sessions and 256 MB of declared
  upload bytes at once. Expired staging is removed when that session is
  touched again or when another upload starts in the project.
  `discard_upload` remains the immediate cleanup path. A project with no
  later upload activity can retain expired partial chunks until the
  maintenance worker is added, but the reservation limits cap that residue.
- New error codes: `UPLOAD_INCOMPLETE`, `CONTENT_HASH_MISMATCH` (§7
  addendum below).
- **Both size caps were lowered after real-world testing, twice.**
  `MAX_UPLOAD_BYTES` started at 10 MB, then 100 MB; `MAX_CHUNK_BYTES`
  started at 4 MB. Both proved undeliverable — real MCP-client tool-call
  size ceilings (ChatGPT's connector included) sit far below what the wire
  format alone allows. Current values: `MAX_CHUNK_BYTES` 256 KB (shared by
  `upload_library_file`'s whole call and one `append_upload_chunk` call —
  each has to fit in a single tool call), `MAX_UPLOAD_BYTES` 20 MB (the
  *assembled* total for a chunked upload, or a §5.3c ChatGPT-fetched file —
  neither of which needs to fit in one call at all).

Total surface with all three experimental additions: **15 read + 12
propose/discard + 17 mutate = 44 tools**. §5.4 adds the two file tools,
bringing the current total to 46.

### 5.3c ChatGPT file-reference upload (experimental, added post-lock)

Added after §5.3b, once it became clear that no `content_base64` size —
chunked or not — reliably survives ChatGPT reproducing it as generated
text. This tool sidesteps the problem instead of mitigating it: ChatGPT's
own `openai/fileParams` MCP host extension lets a tool receive a file the
*connector* has already resolved (a chat attachment or a pick from the
user's File Library) as a temporary, authorized `download_url` — the model
is never in the byte-transport path at all, so there's nothing for it to
garble.

**Generic MCP has no native binary attachment type; this doesn't change
that.** `upload_library_file`/`append_upload_chunk` remain the portable
upload path for every MCP client, including ChatGPT if it prefers them.
`openai/fileParams` is an OpenAI-specific, optional host extension — a
host that doesn't understand it simply won't populate `files` with
resolved references, and core Lattice behavior never depends on any host
understanding it.

| Tool | Params | Behavior |
|---|---|---|
| `upload_library_files_from_chatgpt` | `project, files, folder_path="library"` | Downloads each file's `download_url` directly (SSRF-hardened, streamed, capped at `MAX_UPLOAD_BYTES`) and writes it through the same path as `upload_library_file`; one result per file, partial success explicit. Filenames come from each file's own resolved reference — no caller-supplied names |
| `upload_library_file_from_chatgpt` | `project, file, filename, folder_path="library"` | Same pipeline, one file, stored under a caller-chosen `filename`. The tool to use whenever the destination name matters; failures are plain tool errors (nothing partial to report) |

**Why naming is one file per call.** A parameter listed in
`openai/fileParams` is populated by the ChatGPT *host* during resolution —
the model does not write it. When the model composes the call it has not
seen `file_id` or `download_url`, so it cannot bind a chosen name to a
particular file in a sibling argument: there is no stable handle to name.
That rules out the obvious batch shape (`files` plus a parallel
`file_names: [{file_reference, filename}]` list), because nothing the model
could put in `file_reference` identifies a file. The only remaining way to
match names to files in a batch is *by position*, and positional
correspondence between the model's arguments and the host's resolved array
is an undocumented implementation detail, not a guarantee. `download_url`
is explicitly not identity either — ChatGPT is known to re-resolve one file
to different URLs across retries. Getting this wrong is silent: photos land
under each other's names, every write succeeds, and nothing surfaces an
error. So the batch tool never accepts names (each file's name rides in the
same object as its bytes, which cannot be crossed), and naming goes through
the single-file tool, where one resolved file and one `filename` are
unambiguously related regardless of ordering, URL rewriting, or download
completion order. Several files are uploaded with several calls, safely in
parallel — each takes the project lock only for its own write, and the
download happens outside it. `tests/unit/test_chatgpt_upload.py`
(`TestChatGPTSingleFileIdentity`) holds the identity tests: caller name
beats the suggested name, out-of-order completion under real threads,
duplicate original filenames, identical content under different ids,
rewritten transport URLs, and `file_id` round-tripping into the sidecar.

Note that on ChatGPT mobile the host has been observed sending bare strings
(`"chat_upload"`, `"chat_upload://image_0"`) instead of file objects. Those
fail schema validation and are rejected — fail-closed, since there is no
fetchable URL in them; there is nothing for the server to do but report it.

**File-reference schema (exact, `openai/fileParams` requirement).** The
batch tool declares an array of these; the single-file tool declares one
directly (same object schema, not wrapped in an array):

```json
"_meta": { "openai/fileParams": ["files"] }   // batch
"_meta": { "openai/fileParams": ["file"] }    // single
```

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "download_url": { "type": "string" },
      "file_id": { "type": "string" },
      "mime_type": { "type": "string" },
      "file_name": { "type": "string" }
    },
    "required": ["download_url", "file_id"],
    "additionalProperties": false
  }
}
```

`files`/`file` must be top-level (nested file params aren't supported by
the extension) — verified against the real serialized `tools/list` output,
not just the internal Python object (`core/writes.py`'s `ChatGPTFileInput`
pydantic model represents optional fields as `anyOf: [string, null]`;
`mcp/write_tools.py`'s `_pin_chatgpt_file_schema` overwrites the advertised
schema — `items` on the batch tool, the `file` property itself on the
single-file tool — with the literal dict above after registration, since
FastMCP exposes no public API to edit a tool's schema post-registration —
the same rationale `observation.apply_observation_to_all_tools` already
relies on for reaching into `_tool_manager`).

**Example invocation payload** (what ChatGPT sends):

```json
{
  "project": "garden",
  "files": [
    {
      "download_url": "https://files.openai.com/temporary/authorized/...",
      "file_id": "file_abc123",
      "mime_type": "image/jpeg",
      "file_name": "photo.jpg"
    }
  ]
}
```

Response (partial success made explicit):

```json
{
  "results": [
    {
      "file_id": "file_abc123",
      "filename": "photo.jpg",
      "ok": true,
      "path": "library/photo.jpg",
      "metadata_path": "library/photo.json",
      "mime_type": "image/jpeg",
      "size_bytes": 84213,
      "sha256": "…",
      "error_code": null,
      "error_message": null
    }
  ],
  "succeeded": 1,
  "failed": 0
}
```

**Single-file invocation** — `file` is host-resolved exactly as above;
`filename` is an ordinary model-written argument:

```json
{
  "project": "garden",
  "file": {
    "download_url": "https://files.openai.com/temporary/authorized/...",
    "file_id": "file_abc123",
    "mime_type": "image/jpeg",
    "file_name": "IMG_4242.jpg"
  },
  "filename": "flex-01-front.jpg"
}
```

Response (no partial state to express — errors are raised normally):

```json
{
  "file_id": "file_abc123",
  "filename": "flex-01-front.jpg",
  "operation_id": "…",
  "snapshot_id": "…",
  "path": "library/flex-01-front.jpg",
  "metadata_path": "library/flex-01-front.json",
  "mime_type": "image/jpeg",
  "size_bytes": 84213,
  "sha256": "…"
}
```

Rationale and constraints:

- **A temporary download URL, never a local filepath.** `download_url` is
  authorized and short-lived, resolved and fetched once during this call
  (`core/remote_fetch.py`); it is never logged (only the hostname appears
  in any error message) and never persisted anywhere. `file_id` is an
  opaque identifier only — it is never treated as a URL or fetched
  directly. There is no `filepath`/local-path parameter of any kind; the
  server has no access to, and makes no assumption about, wherever
  ChatGPT's sandbox mounts a file (e.g. `/mnt/data/...`).
- **SSRF hardening (defense in depth), all in `core/remote_fetch.py`:**
  HTTPS only; the hostname *and every redirect target's hostname* are
  independently resolved, and every returned address is rejected if
  loopback, private, link-local, multicast, reserved, unspecified, or an
  explicit cloud metadata-service address (`169.254.169.254`,
  `100.100.100.200`, `fd00:ec2::254`); the TCP connection targets the
  validated IP literally rather than a second, separately-resolved
  hostname lookup, closing the DNS-rebinding TOCTOU window between
  validation and connection (SNI and the Host header still carry the
  original hostname via httpcore's `sni_hostname` request extension, so
  virtual hosting and certificate verification work normally); a fixed
  redirect budget (default 5); response bytes are streamed and counted as
  they arrive, so the cap is enforced against real received bytes, never
  against a `Content-Length` header (present, absent, or lying); connect/
  read timeouts and a total elapsed deadline. The platform DNS resolver is
  not independently interruptible; remote deployments must enforce an outer
  ingress deadline as well.
- **Reuses the existing ingestion pipeline exactly.** Downloaded bytes go
  through the same `_write_uploaded_file` helper as `upload_library_file`/
  `finalize_library_file_upload` — extension denylist, fail-closed
  collision, metadata sidecar (protected fields plus `chatgpt_file_id`),
  snapshot, oplog. `mime_type` is normalized (lowercased,
  parameters stripped) and stored as metadata only; it is never trusted in
  place of the extension check, since there is no content-sniffing step in
  this pipeline for any upload tool to fall back to.
- **Partial success is explicit, not atomic.** One file's failure never
  removes another's result: every requested file gets an entry with
  `ok`/`error_code`/`error_message`, and `succeeded`/`failed` summarize the
  batch. The call itself only fails outright for a malformed batch (empty
  `files`, or over `MAX_CHATGPT_FILES_PER_CALL` — 20 files) or a
  project-level problem, never for one bad file.
- **Aggregate work is bounded.** A batch may download at most 64 MiB total
  and receives a 60-second aggregate wall-clock budget in addition to each
  file's 20 MiB and per-fetch timeout. Remaining byte/time budgets are passed
  into each successive fetch; a later file cannot restart either budget.
- **Not idempotent, on purpose.** Duplicate calls with the same
  `file_id` are not deduplicated — annotations correctly report
  `idempotentHint: false`. Resending the same `file_id` with the same
  derived filename hits the ordinary fail-closed collision check; with a
  different `file_name` it writes a second, independent file. Real
  dedup-by-`file_id` is a possible future addition, not implemented here.
- **Filename resolution (batch).** `file_name`, if given, is sanitized
  exactly like `upload_library_file`'s `filename` (bare name, no path
  separators, no traversal). If omitted, the filename falls back to
  `{file_id}{guessed_extension}`, where the extension is guessed from the
  (normalized, still-untrusted) `mime_type` — that guess only affects the
  filename's suffix, which then goes through the same extension-denylist
  check as every other path, so a lying `mime_type` can't bypass it.
- **Filename resolution (single).** `filename` is required and is the
  caller's decision; the reference's own `file_name` is ignored entirely,
  and there is no `mime_type` guessing. It is sanitized the same way, and
  both the bare-name check and the extension denylist run *before* the
  download, so a rejected name never costs a fetch. `file_id` is still
  recorded in the sidecar (`chatgpt_file_id`) and echoed in the result, so
  a renamed file stays traceable to its origin.
- New error codes: `UNSAFE_URL`, `TOO_MANY_REDIRECTS`,
  `DOWNLOAD_TIMEOUT`, `DOWNLOAD_FAILED` (§7 addendum below) — these appear
  per-file in `results[].error_code`, not as a raised tool error, except
  when they'd otherwise abort a single-file call path (none currently do;
  this tool never raises them itself).
- New dependency: `httpx` (also a transitive dependency of `mcp`; promoted to a direct
  `pyproject.toml` dependency since `core/remote_fetch.py` now depends on
  its API surface directly).

### 5.4 Generic file discovery and retrieval (experimental, added post-lock)

Added after §5.3a–c: uploads could put a file into a project, but nothing
could get one back out. §5.3a's "Known gap: not indexed" is the hole this
section closes — for retrieval. (The indexer, `search_project`, and
`list_tree` remain Markdown-only; `list_files` walks the project instead of
reading an index, so it needs no indexer change and sees out-of-band files
immediately.)

Two tiers, deliberately separate:

| Tier | Surface | Question it answers |
|---|---|---|
| 1 | `list_files`, `read_file` | "What is here, and what can the model actually look at?" |
| 2 | `resources/read` on a `lattice://` URI | "Give me the original, exactly as stored." |

| Tool | Params | Behavior |
|---|---|---|
| `list_files` | `project, path_prefix?, query?, mime_type?, extension?, include_markdown=false, include_sidecars=false, limit=100, cursor?` | Recursively walks the project, returning project-relative paths with MIME type, size, mtime, `resource_uri`, and `context_support`. Deterministic sort and cursor pagination |
| `read_file` | `project, path, max_image_edge=1024, image_quality=78, text_offset=0, max_text_chars=50000` | Returns typed MCP content for one file: a real `ImageContent` rendition, a bounded `TextContent` slice, or metadata only — always with a `ResourceLink` to the untouched original |

**No prescribed location.** A file may live anywhere valid inside a
project. `library/` is where *uploads* land, not where files must be.
Nothing in this section derives meaning from a folder, filename, or
extension: those classify transport only.

**`read_file` vs `read_document`.** `read_document` serves the managed
Markdown surface — frontmatter, `document_sha256`, reconcile-on-read, the
hash guards edits depend on. `read_file` serves bytes generically and knows
nothing about documents. Markdown is *served* by `read_file` as plain text
rather than refused (a `.md` dropped into `library/` out of band is still a
file), but the result sets `is_markdown: true` and
`recommended_tool: "read_document"`, and the tool description says so.
Serving rather than refusing bypasses no controls: reads are ungated on
both paths, and every write still goes through propose/apply.

**`context_support` is a transport classification, not a semantic one.**
Three values only — `image`, `text`, `resource_only`. Introducing values
like "photo", "report", or "evidence" would put domain semantics in the
server, which D13 forbids.

- **Images (`image`).** JPEG, PNG, and WebP are re-encoded into a bounded
  rendition: EXIF orientation applied, aspect ratio preserved, never
  upscaled, longest edge 1024 px by default (caller-bounded 256–4096),
  preferred quality 78 (bounded 40–95), all metadata stripped, and a hard
  64 KiB encoded-byte ceiling. Edge and quality are preferences: encoding
  quality is reduced no lower than 70 (unless the caller explicitly asks for
  less), then geometry is reduced adaptively until the byte limit is met, so
  a caller cannot inflate the result with a retry. The ceiling leaves
  substantial room under Claude.ai's documented approximate 150,000-character
  tool-result limit after base64 expansion, summaries, structured metadata,
  and the resource link. It also keeps the complete result below the smaller
  boundary observed in ChatGPT web testing, whose public documentation does
  not state a raw MCP result limit. The original file is never
  modified. **The original is never returned inline** — a 5 MB photograph is
  far past a web host's useful tool-result size, which is the whole reason
  renditions exist. Transparency forces PNG output; everything else becomes
  JPEG. Decoding is bounded by a header-checked
  100-megapixel ceiling (`FILE_TOO_LARGE`), and an undecodable image is a
  normal `VALIDATION_ERROR`, never a server crash.
- **GIF and SVG stay `resource_only`.** A GIF's first frame is not the
  animation, and presenting it as "the image" would misrepresent the file.
  Rasterizing SVG would mean executing untrusted markup in a rendering
  stack Lattice does not ship. Both remain retrievable in full via Tier 2.
- **Text (`text`).** UTF-8-decodable types are returned as a bounded slice
  with `text_offset`/`max_text_chars` paging, reporting `truncated`,
  `total_chars`, and the `next_offset`. Slicing happens on the decoded
  string, so a window boundary can never split a codepoint. A file whose
  MIME claims text but whose bytes are not valid UTF-8 degrades to
  `resource_only` with `reason: not_valid_utf8` rather than being decoded
  lossily. A type Lattice does not recognize is never speculatively
  decoded as text.
- **Everything else (`resource_only`).** PDFs, Office documents, archives,
  and video return metadata and the resource link, and the summary text
  says the contents have *not* been read. **PDF text extraction, page
  rendering, Office parsing, and OCR are explicitly not implemented.**
  Adding one later means adding a MIME adapter behind `read_file`, not a
  new tool.

**Rich results.** `read_file` is the one tool whose content blocks are not
a single serialized envelope. Image bytes must reach the host as a genuine
`ImageContent` or the host renders base64 as text. The Lattice envelope
still travels in `structuredContent`, so error codes and machine-readable
fields are unchanged. Encoded payloads live **only** in their typed block —
never in the envelope, a text block, or `_meta`.

**Resource URIs (Tier 2).**

```
lattice://file/<project-key>/<base64url-unpadded(project-relative path)>
```

- One shared helper builds and parses these, so a URI minted by `list_files`
  is always resolvable by `resources/read`.
- The path is encoded, not embedded, so spaces, Unicode, punctuation, and
  nested directories survive without a second escaping layer.
- **One canonical spelling per file.** Padded, standard-alphabet, or
  trailing-bit variants are rejected rather than normalized, because clients
  cache and deduplicate resources by URI string.
- No `file://`, no absolute paths, no public URLs, no embedded credentials.
  A project key plus a project-relative path is all a stateless resolve
  needs, and the server-local workspace path never appears in a URI, a
  result, or an error.
- Every read revalidates from scratch: registry lookup, `contained_path`
  resolution, symlink refusal, regular-file check, size cap. A URI is a
  *name*, never a capability.
- `resources/read` returns `TextResourceContents` for UTF-8 text and
  `BlobResourceContents` (SDK-base64) for everything else, always the exact
  original. It never substitutes a rendition and never truncates.

**Resource discovery.** A resource *template* is advertised so clients can
see the URI shape, but `resources/list` stays empty: a project can hold
thousands of files, and enumerating them into a client's resource list is
not a discovery channel. `list_files` is, with filters and pagination.

**SDK note.** The read handler is registered at the low level rather than
through FastMCP's `@resource` decorator, because a FastMCP
`ResourceTemplate` carries a single fixed `mime_type` for every resource it
creates — it could only ever label a JPEG and a PDF identically. Non-Lattice
URIs fall through to FastMCP so nothing else is affected.

**Size cap.** `MAX_RESOURCE_READ_BYTES` is defined once
(`core/file_reads.py`) and starts equal to `MAX_UPLOAD_BYTES` (20 MB):
refusing to serve back a file Lattice itself accepted would be incoherent.
Over the cap fails explicitly with `FILE_TOO_LARGE` carrying the actual
size and the current limit — binaries are never truncated, and a partial
binary is never presented as the original. Text context reads have a
separate, lower ceiling (8 MB) because they decode and slice in memory.
Both are one-line changes once real host compatibility numbers exist.

- New error code: `FILE_NOT_FOUND` (§7 addendum below) — distinct from
  `DOCUMENT_NOT_FOUND`, which belongs to the managed Markdown surface.
- New dependency: `pillow`, for image decoding and re-encoding.
- **Deferred deliberately:** OpenAI tool-file output references,
  Anthropic-specific file output mechanisms, and any other Tier 3
  host-specific integration. Tier 1 + Tier 2 are portable MCP; a host
  extension would be additive on top. The inbound ChatGPT
  `openai/fileParams` upload path (§5.3c) is unaffected — it moves files
  *into* Lattice, this section moves them *out*.

Total surface with the file tools: **17 read + 12 propose/discard + 17
mutate = 46 tools**.

### 5.5 Workspace compacts

Compacts are a workspace-level store for explicit chat handoffs, primarily
for chats that never belonged to a project. The server does not summarize:
`get_compact_instructions` tells the chat agent how to distill the visible
thread and sources, redact or omit secrets by judgment, chunk if needed, and
write through the compact tools. Compact writes are snapshot-protected with
global snapshots and operation-logged under reserved project key
`__workspace__`.

Compact filenames are `compacts/compact_{word}-{word}-{word}-{word}.md`.
On collision the server chooses a different four-word token; no hex suffix is
appended. Frontmatter is compact-specific, not document v2 frontmatter:

```yaml
id: word-word-word-word
created: <iso>
updated: <iso>
project: <key-or-null>
state: draft        # draft | finalized | resumed | archived | stale
resume_count: 0
handoff_prompt: null
sources: []
tags: []
document_sha256: null
```

After finalization, `state` becomes `finalized` and the body begins with a `## Handoff Prompt` block
containing the exact prompt subsequent chats must follow, then the compact
summary sections. Compacts are list/read/resume only in v1: no compact search
and no inclusion in project context.

## 6. Out-of-band edits (D12)

Hand-edits on disk are first-class. Two mechanisms:

1. **Reconcile-on-read (correctness floor).** Every read that serves content
   or maps (`get_context`, `read_document`, `read_document_range`,
   `get_document_map`, `find_in_document`, `search_project`) compares
   on-disk mtime+size against the index; on drift it (a) rehashes and
   reindexes the file, (b) marks pending proposals bound to the old
   `document_sha256` as `stale` (subsequent `apply_patch` →
   `PATCH_CONFLICT` with `reason: "out-of-band-edit"`), (c) writes an oplog
   entry `source: out-of-band`. The mtime check must be cheap enough to run
   on every read (stat only; hash only on drift).
2. **Watcher (liveness).** The existing watchdog worker, in watch mode,
   debounces filesystem events per file (coalesce window: 5 s of quiet, max
   one snapshot per file per 60 s) and takes a **snapshot-on-detect** plus
   reindex + oplog entry. Snapshot publication precedes reconciliation so a
   snapshot failure cannot consume the indexed drift signature. Known
   transient filesystem/database failures are requeued, snapshot rate-limit
   reservations are released on failure, and both pending-event and
   rate-limit maps are bounded. Watcher failure modes (server down during edit,
   synced mounts, rename-based saves, event overflow) are covered by (1).

## 7. Errors

Envelope unchanged. Full v2 code list:

Boundary fallback: `INTERNAL_ERROR` is returned with a correlation id when
an unexpected exception reaches the MCP boundary. The exception message and
document/tool arguments are not exposed to the caller or observation log.

Kept: `VALIDATION_ERROR`, `DOCUMENT_NOT_FOUND`, `PATCH_CONFLICT`,
`PATCH_PROJECT_MISMATCH`, `WORKSPACE_MISMATCH`, `FRONTMATTER_PROTECTED`,
`SNAPSHOT_NOT_FOUND`, plus the existing granular patch-target codes.

New: `PROJECT_REQUIRED`, `PROJECT_NOT_FOUND`, `PATCH_EXPIRED`,
`DOCUMENT_ARCHIVED`, `UNKNOWN_FOLDER`, `CANNOT_ARCHIVE_SPINE`,
`PATH_EXISTS`, `FORMAT_UNSUPPORTED` (workspace format
mismatch, [spec-versioning.md](spec-versioning.md) §1.2 — reads allowed on
an older workspace, writes refused; everything refused when the workspace
is newer than the server supports).

Deleted: `SESSION_REQUIRED`, `SESSION_MISMATCH`, `SESSION_NOT_FOUND`,
`WORKSPACE_LOCKED`.

New (§5.3a, `upload_library_file`, experimental): `UNSUPPORTED_FILE_TYPE`
(blocked upload extension), `FILE_TOO_LARGE` (decoded payload — whole file
or, for the chunked path in §5.3b, a single chunk or the running total —
over its respective cap; see `details.scope`).

New (§5.3b, chunked upload, experimental): `UPLOAD_INCOMPLETE` (finalize
called with missing chunks), `CONTENT_HASH_MISMATCH` (assembled bytes don't
match a caller-supplied `expected_sha256`).

New (§5.3c, ChatGPT file-reference upload, experimental — appears per-file
in `results[].error_code`, not as a raised tool error): `UNSAFE_URL`
(non-HTTPS, unresolvable host, or SSRF-unsafe address — initial URL or any
redirect hop), `TOO_MANY_REDIRECTS`, `DOWNLOAD_TIMEOUT` (connect/read/total
timeout), `DOWNLOAD_FAILED` (network error or non-2xx response).

New (§5.4, generic file surface, experimental): `FILE_NOT_FOUND` — a
project-relative path resolves to nothing readable. Deliberately distinct
from `DOCUMENT_NOT_FOUND`, which belongs to the managed Markdown surface;
an agent that gets `FILE_NOT_FOUND` should re-run `list_files`, not
`list_tree`. `FILE_TOO_LARGE` is reused for a file over
`MAX_RESOURCE_READ_BYTES`, a text file over the text-context ceiling, and
an image over the decodable-pixel ceiling — `details` carries
`size_bytes`/`limit_bytes` (or `max_pixels`) so the caller can tell which.
`VALIDATION_ERROR` covers an undecodable image, a directory or special
file, and a malformed or non-canonical `lattice://` URI.

**Errors on `resources/read` have no Lattice envelope.** The MCP resource
protocol carries only a JSON-RPC error, so the machine-readable code rides
in that error's structured `data` as `error_code`, alongside the same
`details` fields the tool envelope would have carried.

## 8. Observation log & DB changes

- Observation entries lose `session_id`; gain `correlation_id`
  (server-generated per call), `client_name`/`client_version` (from MCP
  initialize metadata when the transport exposes it; "Not exposed"
  otherwise), `transport`, `result_bytes`, `duration_ms`.
- `get_context` calls additionally record `rules_bytes`, `spine_bytes`,
  `documents_count` (§4 telemetry).
- `list_files` records `count`, `scanned_count`, `has_more`. `read_file`
  records `representation`, `context_support`, and the original vs
  rendition `mime_type`/`size_bytes`/dimensions — so a host's real payload
  ceiling can be found from the log rather than guessed. `resources/read`
  is observed under tool name `resources/read` with `mime_type`,
  `size_bytes`, and `kind` (`text`/`blob`). All of it is metadata: no file
  content, no blobs, no path values.
- Operations table: `session_id` column dropped (fresh start — a plain
  schema migration, no data carried); pending proposals keyed by
  `operation_id` with `project`, `path`, `base_sha256`, `created_at`,
  `expires_at`, `state: pending|applied|discarded|stale|expired`.
- Schema changes ship through the numbered-migration framework
  ([spec-versioning.md](spec-versioning.md) §2.4: `PRAGMA user_version`,
  `db/migrations/`, auto-applied at startup) — the ad-hoc `_add_column`
  calls in `db/database.py` are replaced by it.
- Dropped tables (spec-versioning §2.2): `sessions`, `canvases`, plus the
  write-only `projects`, `document_blocks`, `document_links` (registry
  truth is `system/projects.yml`; blocks/links return only as real read
  features).
- `search_index` becomes FTS5 with bm25 ranking and porter stemming
  (spec-versioning §2.3); `search_project` returns real scores and FTS
  snippets.
- Redaction rules unchanged: metadata only, never content, secrets →
  `[redacted]`.
- The v1 session code (mcp/session tools, `core/sessions.py`, and session
  plumbing in writes/operations) was removed with the
  12 Jul fresh-space cleanup along with the rest of the v1 implementation;
  v2 code must never reintroduce it.

## 9. `initialize` instructions (condensed bootstrap)

Exact string:

> Lattice is the user's shared Markdown workspace and the source of truth
> across chats. If the user explicitly invokes `/compact`, `@lattice
> /compact`, or asks for a Lattice compact, call `get_compact_instructions`.
> If the user invokes `/resume <token>` or asks to resume a Lattice compact,
> call `resume_compact`. For project work, call `get_context` with your
> project key before anything else, and obey the rules it returns. Never use
> compacts for ordinary project memory, notes, summaries, or document
> updates. Propose-then-apply for every edit; a propose result is not a saved
> edit. A project also holds non-Markdown files (photographs, PDFs,
> exports). They are not in get_context and not searchable by content. To
> work with one: read the project's rules, spine, and documents first — they
> carry the workspace's own conventions and often reference files by path;
> call `list_files` when you do not already know the path; call `read_file`
> to put a supported representation (image rendition or bounded text) into
> context; and use the `resource_uri` it returns when you need the exact
> original. There is no required folder for files, and a file's meaning
> never follows from its folder, filename, or extension alone — read the
> documents that reference it.

The file paragraph is deliberately about *how to find and read* files, not
about what any file means. Binary files are **never** part of `get_context`:
a project can hold thousands, and putting them in the contract call would
make the startup payload unbounded — exactly what §4's telemetry exists to
prevent.

## 10. Acceptance criteria

1. A fresh chat needs exactly one call (`get_context`) to receive rules +
   spine + map; no tool requires any prior call except `apply_patch`
   (requires a `propose_*`) — verified by an integration test that calls
   every tool cold.
2. Every scoped tool rejects missing/unknown `project` with the right code.
3. `apply_patch` after an out-of-band edit of the target returns
   `PATCH_CONFLICT`, never clobbers (adversarial test: edit file on disk
   between propose and apply).
4. `archive_document` + `unarchive_document` round-trip preserves content,
   id, and history; archived docs vanish from `get_context.documents` and
   default search.
5. No tool result ever contains a session id; grep-level check that
   `session_id` is gone from `src/lattice/mcp/`.
6. Observation log rows for `get_context` carry the three payload metrics.
7. All path handling passes the existing adversarial path/symlink test
   suite under the new layout.
8. (§5.4) An agent with no prior knowledge of a project's file layout can
   call `list_files`, discover an arbitrarily nested photograph, pass the
   returned project-relative path to `read_file`, and receive a real MCP
   `ImageContent` rendition plus a `ResourceLink`; issuing
   `resources/read` on that URI returns bytes identical to the stored
   original. A ~5 MB photograph succeeds without the original appearing in
   the tool result. A PDF is discoverable and retrievable exactly through
   Tier 2 without implying PDF analysis happened. The typed blocks are
   verified at the protocol level through a real in-process MCP client, not
   through the Python functions. Model visibility remains a host-level
   compatibility check and is not proven by protocol deserialization alone.
