# Workspace lint

`ferumind lint` is a local operator check for mechanical workspace problems.
It reports; it does not repair, score, rank, or interpret knowledge.

```bash
uv run ferumind lint
uv run ferumind lint --project notes
uv run ferumind lint --json
uv run ferumind lint --severity warning
```

`--severity` is a minimum: `info` shows everything, `warning` hides advisory
info findings, and `error` shows errors only. The command exits non-zero when
the unfiltered run contains an error; warnings and info findings alone exit
zero. JSON is deterministic and contains the same typed findings as the human
view, including project, path, line when known, check id, severity, and a
bounded message.

## What it checks

- current-format managed frontmatter and required descriptions;
- duplicate managed-document ids within a project;
- unsafe, missing, archived, and fragment-bearing Markdown links;
- Markdown documents outside the legal role folders;
- derived index inconsistencies that remain after reconcile-on-read has had a
  chance to converge them.

Internal links use project boundaries. A relative target starts at the citing
document's directory; a role-prefixed target starts at the project root. A
target that names an existing directory — a trailing-slash folder reference in
a spine document map, for example — resolves cleanly and is not a finding.
External URLs are neither fetched nor validated. Source links receive the same
mechanical checks as every other link; lint does not infer provenance
completeness from a heading or count citations.

`archive_document` moves a document under `archive/` and rewrites no link, so
staleness appears in both directions — but only one of them is repairable, and
lint reports only that one:

- **A live document cites a target that has since been archived** (or names a
  stale `archive/` path for something still live). The citing document is
  editable, so this is a `warning` naming where the link does resolve.
- **An archived document's own links** resolve one level too deep, into an
  `archive/` mirror that was never created. Nothing is reported: archived
  documents are a hard refusal (`DOCUMENT_ARCHIVED`) and can never be edited,
  so the finding would name a repair nobody can perform.

A link that resolves on neither side of the archive boundary is still an error,
including from an archived source — a target that exists nowhere may be lost
data, and restoring it is an action even when editing the citing page is not.

Inline links, full/collapsed/shortcut reference links, image destinations,
multiline inline links, and URI/email autolinks are parsed structurally. Links
inside code are ignored, and bare URLs are ordinary text. Image destinations are
validated as file references. `ferumind://` destinations are rejected: durable
knowledge uses project-relative paths, and cross-project links are outside the
project boundary. Fragments are warnings only and are compared with the
canonical derived section ids.

## What it deliberately does not check

Lint reports only what can be mechanically established and acted on. It makes no
judgement about whether a document is *useful*, *well-organized*, or *reachable
by a reader*.

In particular there is **no orphan or "uncited document" check**. One shipped
briefly and was removed: it counted inbound Markdown links, which measured
authoring syntax rather than curation, and a workspace that referenced documents
as backticked paths registered as having no links at all. `get_context` already
delivers every document with its `description`, so an unlinked page is not an
unreachable one. If a reference check returns, it will be because a real
workflow needed it, with evidence.

## Mutation boundary

Lint never opens a Markdown document for writing and has no `--fix` mode. It
does run ordinary reconcile-on-read before checking the index, so an
out-of-band edit can refresh derived SQLite rows, stale a pending proposal,
and add the normal metadata-only `source: out-of-band` operation record. That
is the same behavior as project-wide reads, not an automatic document repair.

Lint is intentionally CLI-only. It is not an MCP tool, background job, `just
verify` step, or CI gate: live user data must not become a source-build
dependency. The existing `just lint` recipe continues to mean Ruff; use the
fully qualified `uv run ferumind lint` command for workspace lint.
