# What is Ferumind?

Locked 11 Jul 2026. This document asserts; it does not deliberate. Specs are
written against this document; superseded deliberation is not retained.

## Identity

**Chats are disposable; Ferumind is where the continuity lives.**

Every chat is a goldfish — brilliant for an hour, then gone. Ferumind is the
durable brain underneath: a local-first Markdown workspace holding shared
working state, agent memory, and the knowledge that settles out of real work.
The MCP server's job is to make any agent, in any client, in any future chat,
behave like it's been on the project all along.

Three scenes:

1. **Working tempo.** You finish a garden and tell whatever chat is handy
   "bench 3x8 at 20 kg". The agent gets its contract from the server and,
   without being told, logs the session, preps the next one, updates the goal
   estimate, and notes continuity in memory. Nothing re-explained.
2. **Library tempo.** Two years on, the home-lab dies. Fresh chat, any
   client: "walk me through the rebuild." The agent finds the runbook that was
   distilled from the actual working sessions — gotchas included, because they
   were captured when they happened.
3. **Lifecycle.** A training phase ends. The agent distills what still matters
   into memory and library, then archives the raw working doc. Search stays
   clean, nothing is lost, and "what was my bench in July '26?" still has an
   answer.

Ferumind makes agents **resumable** and knowledge **cumulative** — across
clients, chats, and years.

## Design principles

1. **Documents carry the intelligence; the server is a librarian.** Behavior
   lives in documents and in agents honoring them. The server's jobs are
   mechanical: protect (paths, hash guards, snapshots, no hard delete),
   index/search, and assemble context — read-and-concatenate, never decide.
   The honest, closed list of exceptions where the server does enforce:
   identity frontmatter keys are protected, and writes against archived
   documents are refused. Anything else an agent is told, not blocked from.
   Server-side enforcement of document policy is a fallback we adopt only if
   real usage shows agents can't be trusted to honor it — not a design goal.
2. **This is the agents' space — pave cowpaths, don't fence the field.**
   Folders and policies are shared vocabulary and default homes, not a
   straitjacket. Agents are free to create new memory files, new canvases,
   new structure — and the contract tells them so. Hard rules are few and
   safety-shaped: project boundary, no hard delete, memory never leaks into
   shared documents.

## Decided

### D1 — One product, two tempos

The working space (live collaboration: plans, logs, memory) and the library
(durable reference: runbooks, playbooks, decisions) are one product, because
working sessions are where library content gets made and the library is what
makes working sessions smart. Known risk, stated honestly: the distillation
flywheel has exactly one existence proof (the recipe library), and the
home-lab project articulated the loop verbatim and still stalled at one
catch-all file. Whether agent-nudged distillation actually happens is a thing
dogfooding must show (see D10).

### D2 — Project structure

```
workspace/
  AGENTS.md            ← generated pointer for filesystem-native agents
  compacts/            ← workspace-level chat handoff compacts
  system/
    rules/             ← workspace-level rules (how any agent behaves here)
    skills/            ← on-demand Ferumind skills, fetched by trigger (D7)
    prompts/           ← the bootstrap prompt
    schemas/, templates/
  projects/<key>/
    spine.md           ← the one entry page: orientation, precedence rule, map
    rules/             ← project-specific standing rules
    canvases/          ← live collaborative working documents, incl. logs
    memory/            ← agent memory: a folder, shared by all agents
    library/           ← docs, playbooks, runbooks, reference, decisions
    inbox/             ← capture buffer, meant to be empty
    archive/           ← mirrors the structure things came from
```

**The folder is the role.** There is no `role:` frontmatter enum; location
says what a document is, and the contract explains what each folder means.
Folders nest freely — internal organization belongs to the humans and agents,
not the schema.

`compacts/` is deliberately outside `projects/`: it holds explicit chat
handoff compacts for free-floating conversations that do not belong to a
project. A compact is not a project document and does not appear in project
context or search.

### D3 — Frontmatter (minimal)

Two behavioral axes on top of the existing identity keys
(`id`/`type`/`project`/`title`/`created`/`updated`):

```yaml
status: active | gated | frozen | archived
edit_policy: free | append | propose-first | ask-human   # optional
```

Gate conditions, cadences, and freshness hints stay prose (in the document or
the spine) until real usage demands more. Every additional axis is another
thing an agent can get wrong; vocabulary is added when its absence hurts, not
speculatively.

### D4 — Canvas, reclaimed

"Canvas" means exactly: a live collaborative working document — the surface
both parties draw on. Memory, rules, and library documents are not canvases.
Logs are canvases with `edit_policy: append` that roll over by calendar
(monthly files), never by phase — a log outlives every plan, which is what
makes plans archivable.

### D5 — Memory

Per-project `memory/` folder, shared by all agents on the project; agents may
create and organize files as they see the need. Hard rule: memory never leaks
into shared documents. Memory is compacted (roll old notes into summaries,
archive the raw file), never deleted. Concurrency is handled by the existing
hash-guarded patches — a conflict fails clean.

The folder carries two layers. **Curated memory** is what an agent should
remember and act on; it keeps every behavior described above, including its
ability to be prescriptive. **Episodes** are what happened: decisions and the
reasoning available at the time, incidents, corrections, experiments and
their outcomes, approaches that failed. Episodes are historical evidence, not
standing instructions — a later agent may reason from one, but recording it
never made it authoritative, and it does not override current state.

Episodes live in `memory/episodes/YYYY-MM.md`, one document per calendar
month, created the first time one is recorded and never seeded empty. That is
a subdirectory of an existing role folder, so it needs no new top-level
folder, no `role:` key, and no format bump, and a project that records none
is indistinguishable from one on the previous contract. An episode is often
what D6 distillation later consumes — the gotcha captured when it happened
rather than reconstructed afterwards.

### D6 — Archive and distillation

Archive is a lifecycle state first (`status: archived`), a folder move second
(mirror path under `archive/`); the archive tool does both, and reconcile
treats the frontmatter as truth if they ever disagree. Archived documents are
excluded by default from search, maps, and context. Archiving is a
distillation trigger: fold what still matters into memory or library, then
archive. Distillation is human-initiated and agent-nudged — the agent suggests
it at natural moments (phase end, bloated canvas, re-derived fact); no
background job, because distillation requires judgment and a conversation.
Evidence-derived, load-bearing library claims keep ordinary Markdown
`## Sources` links, applied forward rather than fabricated for history;
`description` says what a document is for, not where its claims came from.
Archived source paths resolve through the archive mirror. Sources remain
evidence rather than instructions, and the server neither follows them nor
judges whether they support a claim.

### D7 — Rules layering and ownership

Workspace rules apply everywhere; project rules add or override; the context
call returns the merged contract (concatenation with source headers, no
semantic merging). Rules are the human's files — `edit_policy: ask-human`,
always. Agents edit them only on explicit request in-conversation, but are
expected to *recommend* changes when they notice repeated corrections. Skills
are on-demand procedures with triggers and an index, and they are **built**:
`system/skills/` is the sibling `rules/` was designed to grow, `get_context`
carries a name-plus-trigger index, and `read_skill` fetches a body when the
trigger matches. **Due-ness is deliberately not built** — no cadence
frontmatter, no `last_run`, no due-now reporting. Every trigger in real use is
situational rather than temporal, and per-agent mutable state has no home in a
server that is stateless per call.

### D8 — No sessions; one connector; stateless per call

The session model (`start_session`, `session_id` threading, project-switch
locking) is removed. It made the model responsible for a binding that
configuration should carry, and the MCP 2026-07-28 revision removes protocol
sessions anyway.

- **One connector, one URL, registered once.** The bootstrap prompt carries
  the project key — its only variable; every scoped tool takes an explicit
  `project` argument the prompt pins. No per-project settings dance.
- **The server is stateless per call.** Every call carries what it needs:
  `project`, patch/operation ids, hash guards. `propose_*` returns a
  short-lived operation id bound to project + document hash; `apply_patch`
  re-validates. The id + guards are the binding; there is nothing to forget,
  expire, or recover.
- Path-scoped endpoints (`/mcp/<key>`) are a future option for headless
  machine clients whose URL is config in code — never something a human sets
  up per project.
- Server-side sessions shrink to bookkeeping (proposal binding and observation
  log correlation).

### D9 — Bootstrap prompt

Minimal, generic, never-changing, one variable. Canonical copy lives at
`workspace/system/prompts/bootstrap.md`; the server's `initialize`
instructions carry a condensed echo. The shape:

> You collaborate with the user through Ferumind, a shared Markdown workspace,
> via the Ferumind MCP server. The workspace — not this chat — is the source of
> truth and the continuity between chats. Your project is `<PROJECT_KEY>`;
> pass it on every Ferumind call and never name another project. At the start
> of every chat, call `get_context` and obey what it returns — it outranks
> this prompt, your defaults, and your chat memory. Look facts up instead of
> trusting chat memory; record what's worth keeping back into the workspace.
> If Ferumind is unreachable, say so before advising from memory.

`get_context` returns the merged rules, the spine, the document map, the skill
index (D7), and the inbox count. Size discipline (decided 11 Jul): **uncapped
to start, observed** — every call logs its payload sizes to the observation log
and echoes the same numbers in the result's `payload` field, so they are
visible in transcripts too; a cap is a later decision made from that data,
because the fat-prompt risk is real but the right number isn't guessable in
advance.

### D10 — Validation is dogfooding

No formal gate, no lab experiment. The new layout and contract go live on the
real projects (garden first) and get judged by use: do fresh chats follow the
contract, honor `ask-human`, keep memory clean, without hand-holding? If
agents prove unreliable at honoring declared policy, the fallback is
server-side enforcement (principle 1's escape hatch) — a change to make from
evidence, not in advance.

### D11 — Threat model, stated honestly

Amended 30 Jul 2026 for the public alpha: the only supported transport is
single-user local stdio. The source repository may be public, but a checkout
containing a live workspace and its MCP service must not be exposed remotely.

The OpenAI Secure MCP Tunnel is outbound-only: it avoids an inbound port and
public hostname, but it still grants remote read/write access through a relay.
Account scoping and a `tunnel_id` are not server-verified identity or
workspace authorization.

Amended 4 Aug 2026: a workspace owner may run the tunnel against their own
live workspace for single-user development, accepting that the relay and its
credentials are the only access control. The launcher no longer refuses this
mechanically, because a gate that blocks the project's only working remote
path gets reverted rather than respected. The gate below still governs what
is *supported* — remote serving for any subject other than the owner running
it themselves requires all of:

- OAuth validation for ChatGPT and Claude at the server boundary;
- deny-by-default subject-to-workspace-owner authorization;
- ingress limits, egress policy, secret isolation, monitoring, encrypted
  backups, revocation, and independent deployment review.

OpenAI or another relay provider remains in the data path: document and memory
payloads transit that provider. This must be an explicit operator choice after
the remote gate passes. Local stdio clients on the same machine are unaffected.

Behind the perimeter, single-user assumptions hold: the project boundary is
convention + audit + snapshots + no-hard-delete — protection against
accidents, not adversaries. Per-project capability tokens are the sharing
milestone's problem; a second user is the first actor who can cross a project
boundary on purpose.

### D12 — Out-of-band edits are a first-class path

Hand-editing on disk (vim, Obsidian) is core, not an edge case.
**Reconcile-on-read** is the mechanism: every core read mtime-checks; on drift
it reindexes, invalidates pending proposals bound to the old hash, and logs
`source: out-of-band`. An agent never acts on a stale copy at the point of use.

**Decision, 2026-08-06: the filesystem watcher is removed.** The original
design paired reconcile-on-read with a debounced snapshot-on-detect watcher as
a liveness layer, on the theory that some edits need catching before the next
read. Operational evidence says they do not: across ~4 weeks and 2,241 recorded
operations, 4 were out-of-band (0.18%, roughly one a week) and reconcile-on-read
caught every one. A supervised background process, a runtime dependency, and a
restart/failure story is not a proportionate answer to a weekly event that is
already covered. The watcher was never wired into a lifecycle, so nothing that
worked stopped working.

The trade is explicit and accepted: an out-of-band edit is now detected at the
next read that touches the path, not within a debounce window. The recovery
point (snapshot-on-detect) is therefore not captured for a file that is edited
and then never read again. Snapshot-before-mutation still protects every write
Ferumind makes.

### D13 — Workers and agents

The judgment-shaped agent stubs (triage, docs, runbooks, decisions) dissolve
into procedures executed by whichever chat agent is connected — the chat
agent *is* the agent. Mechanical work (indexing, maintenance) stays: no LLM,
no judgment — but it runs inline on the calling path, not in a background
worker. As of 2026-08-06 there is no workers layer at all: the watcher was
removed (D12) and the backup/link-checking workers were never built.
Headless autonomous agents are
parked behind a cost-control gate; when they come, they are just another MCP
client obeying the same contract — zero design change.

### D14 — Fresh start

Built clean (decided 11 Jul): new workspace layout, new tool surface, no
compatibility shims, no dual surfaces. The pilot project was *recreated*
under the current layout rather than migrated. That dissolved the
cutover/split-brain question which had blocked the first spec attempt.

Nothing was carried across, and nothing is left to carry: there is no
importer for the preceding layout and none is planned. A workspace moves
between formats through `ferumind migrate` (spec-versioning §1.3) or not at
all.

### D15 — Version the workspace, not the API

Decided 12 Jul, so the next format never needs a rebuild. The workspace
format (folders, frontmatter contract, `system/` files) carries an
explicit version — currently `format: 1` in `workspace/system/meta.yml`, at
whole-workspace granularity. Format 1 is the floor: it is the first layout
published to anyone, and nothing precedes it to migrate from. The
server supports exactly one document contract: older markers keep read
entrypoints open for semantically prepared documents, while writes refuse
with `FORMAT_UNSUPPORTED` until a human runs `ferumind migrate`
(snapshot- and backup-protected; never implicit). There is no second legacy
parser; an unprepared document that violates the current contract fails
closed. The MCP surface is not
wire-versioned — chats are disposable, so there are no long-lived clients to
break: tools stay stable and additive within a format, `get_context`
re-teaches every fresh chat, and breaking tool changes ride a package version
bump. The standing rule: **a format bump is not done until its migration is
tested and proven in the same change.** The migrator ships, every time —
the single-workspace exception that predated publication is spent. SQLite
stays (right engine, was just under-finished): schema versioned by numbered
migrations, search upgraded to FTS5, dead write-only tables dropped. Full detail in
[spec-versioning.md](spec-versioning.md).

## Evidence, honestly weighted

The design generalizes one user's observed usage: the garden project's agent
independently built a spine, a private memory file, standing rules, gated
plans, and a precedence model; research-notes invented the same split
independently two weeks earlier; finance/home-lab/coursework are library-tempo
with a source-of-truth index. That is **two sustained-collaboration projects,
one user, convergent** — real signal, small n. It earns "this is the right
default structure," not "this is a law of the medium." The home-lab
project's stall (flywheel articulated, never executed) is the standing
counterexample D1 and D10 must answer in use.

## Open

- Migration plan for the old projects — deliberately deferred until after
  dogfood (D14).
- `get_context` size cap — deferred until the telemetry says what normal
  looks like (D9).
- Log ergonomics: cheap "last N entries" reads across a monthly rollover
  boundary.
- Skills due-ness: cadence frontmatter, `last_run`, and due-now reporting.
  Index delivery and triggers shipped; due-ness waits for a genuinely
  time-based skill to justify per-agent state.
- Freshness metadata (`as_of`) for reference docs — only if prose warnings
  keep hurting.
- Semantic/embedding search — parked post-dogfood (D15 forks chose "FTS5 now
  + plan embeddings"): revisit only if FTS5-quality retrieval proves
  insufficient in real use; needs an embedding source and chunking policy.
- Browser lookup degradation in fat-context chats: re-test once the slim
  bootstrap + targeted `get_context` land.

## The folder

- [spec-mcp.md](spec-mcp.md) — the MCP surface: scoping, tool inventory with
  schemas, frontmatter, errors, statelessness, acceptance criteria.
- [spec-flows.md](spec-flows.md) — the end-to-end behaviors the specs add
  up to.
- [spec-versioning.md](spec-versioning.md) — workspace format versioning,
  `ferumind migrate`, DB migration framework, FTS5 search, table cuts (D15).
- [contract/](contract/) — the actual contract text: workspace rules,
  bootstrap prompt, seed templates (source of record; installed by
  bootstrap).
- [roadmap.md](roadmap.md) — build phases 0–4, dogfood exit criteria.
