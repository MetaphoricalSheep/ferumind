# Operator observability

Ferumind's diagnostic path is local, metadata-only, and useful even when the
MCP process is offline:

```text
MCP call metadata ── SQLite observations ─┐
                                         ├─ typed core diagnostics ─ CLI
exception/lifecycle metadata ─ JSONL ────┘                         └ dashboard API/UI
```

The CLI and dashboard call the same core query layer. Diagnostic SQL does not
live in either presentation layer, and the dashboard never initializes,
migrates, or writes the observation database.

## Commands

Start with a compact summary:

```bash
uv run ferumind doctor
```

Investigate observations without opening SQLite:

```bash
uv run ferumind observations list --since 1h
uv run ferumind observations errors --since 24h
uv run ferumind observations show fm_corr_0123456789abcdef
uv run ferumind observations --project notes --tool get_context list --json
uv run ferumind observations errors --error-code INTERNAL_ERROR --json
```

Workspace, project, tool, and client scope options precede the observations
subcommand; time, result, error-code, limit, and JSON options follow it. Run
`ferumind observations --help` and the selected subcommand's `--help` for the
two concise option groups.

Correlation IDs are opaque whole strings. Current `fm_corr_...` IDs and
historical `lat_corr_...` IDs use the same exact lookup path; prefixes are
never parsed as a version or dispatch key.

Start the dashboard in the foreground:

```bash
uv run ferumind dashboard
# http://127.0.0.1:8765
```

Use `--open` to ask Ferumind to launch the default browser, `--port` to choose
another loopback port, and `--workspace` to inspect a specific workspace.
Ctrl+C stops the operator process cleanly.

### "Diagnostics are partially available"

This banner means one or more diagnostic sources could not be read; it does
not mean that all Ferumind activity is unhealthy. The dashboard remains
read-only and shows every source that is available. To print the exact source
issues for the same workspace, run:

```bash
uv run ferumind doctor --workspace /path/to/workspace
```

On the first dashboard run after upgrading, the usual issue is a missing
private runtime-event log. The dashboard does not create that log. Restart the
updated MCP server (or its tunnel), then make an MCP call; the server creates
`workspace/.ferumind/logs/ferumind.jsonl` and records its lifecycle there.
Historical SQLite call observations remain available while the log is absent.

## Local-only security model

The dashboard always binds `127.0.0.1`; there is deliberately no `--host`
option. It accepts only expected localhost Host forms, serves a fixed package
asset allowlist, supports read-only GET/HEAD requests, sends a self-only
Content Security Policy and related browser hardening headers, and exposes no
CORS or mutation surface. Stored metadata is rendered as untrusted text.

The dashboard is separate from the MCP stdio process and from the outbound MCP
tunnel. Never publish it through that tunnel or a public reverse proxy. To
administer Ferumind on another machine, keep the server loopback-only and use
SSH port forwarding:

```bash
ssh -L 8765:127.0.0.1:8765 operator@ferumind-host
```

Then open `http://127.0.0.1:8765` locally while the SSH session is active.

## What is recorded

Normal MCP calls produce one SQLite observation with safe metadata such as
time, tool, project, success/error code, client identity when exposed by the
transport, duration, result size, argument *key names*, and bounded context
metrics. They are not duplicated into the runtime log.

Exceptional and lifecycle events append to:

```text
workspace/.ferumind/logs/ferumind.jsonl
```

The directory is `0700` and the file is `0600`. Events cover process start and
stop boundaries, client initialization, transport close, malformed/oversized
requests, internal errors, and observation-persistence failures. An internal
error may record its exception type, a stable fingerprint, and stack locations
containing only module, repository/package-relative source path, function, and
line number.

Neither store records or reconstructs:

- MCP argument values, raw requests, or raw results;
- document bodies, patch content, or file contents;
- exception messages, traceback text, locals, or arguments;
- authorization headers, tokens, signed URLs, or remote response bodies.

Observability is non-interfering: failure in result interpretation, metric
extraction, sizing, observation persistence, or runtime-event persistence does
not replace a successful MCP result. When possible, a safe fallback event keeps
the correlation ID and tool name even if the SQLite row could not be written.

## Reading lifecycle state

Lifecycle events distinguish a process that never recorded a start, a started
process, a client initialization, observed MCP activity, a closed transport,
and a clean stopping event. They do not prove why a process disappeared. The
CLI and dashboard therefore report uncertain or incomplete state explicitly
instead of labelling an absent stop event as a crash.

Missing or unreadable SQLite, a missing runtime log, malformed JSONL lines, and
an offline MCP server appear as scoped degraded states. They do not prevent the
dashboard itself from starting.

## Dashboard design system

The UI consumes exact committed copies of Basecoat's `tokens.css`, `base.css`,
and `components.css`. They live under
`src/ferumind/dashboard/static/basecoat/`; `REVISION` records the source commit
and the adjacent README records provenance. No CDN, frontend framework, Node
runtime, or build step is involved.

Refresh the snapshot from a reviewed, clean Basecoat checkout:

```bash
just sync-basecoat /path/to/basecoat
```

The sync script performs no network access. It refuses relevant dirty source
files, copies the canonical CSS byte-for-byte, and updates the pinned revision.
Review the resulting theme and provenance diff before committing it.

## Accessibility review

Repository tests enforce one page heading, semantic landmarks, labeled native
controls, table headers, non-color chart cues, reduced-motion rules, visible
focus foundations, local-only assets, and safe DOM rendering. Those structural
checks cannot prove computed contrast, zoom/reflow quality, or screen-reader
announcements in every browser. A release review should therefore also traverse
all five views and the observation detail with a keyboard, inspect both themes
at 200% zoom, and exercise the chart summary and live notices with a screen
reader. Ferumind does not import Basecoat's Node toolchain for these checks.
