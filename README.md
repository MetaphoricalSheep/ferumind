# Ferumind

> Chats are disposable; Ferumind is where the continuity lives.

[![CI](https://github.com/MetaphoricalSheep/ferumind/actions/workflows/ci.yml/badge.svg)](https://github.com/MetaphoricalSheep/ferumind/actions/workflows/ci.yml)

Ferumind is a Markdown workspace shared by you and your AI agents. It runs on
your own machine, the documents stay readable and editable on disk, and a
stateless MCP server handles project scoping, retrieval, guarded edits,
snapshots, and an auditable history of every change.

It is built to be used from a web chat client. An outbound relay dials from
your machine to ChatGPT, so the workspace is reachable from the chat you are
already in, with no inbound port and no public hostname. Local clients like
Claude Code and Cursor connect over stdio.

**Beta**, distributed as a source checkout, on Python 3.12-3.14. The product
contract is in [`product/`](product/00-what-is-ferumind.md); where code and
[`product/spec-mcp.md`](product/spec-mcp.md) disagree, the spec wins.

## Quick start

Linux, Python 3.12-3.14, [`uv`](https://docs.astral.sh/uv/), and
[`just`](https://github.com/casey/just). [`AGENTS.md`](AGENTS.md) lists the
plain `uv` equivalent of every `just` recipe.

### 1. Install

```bash
git clone https://github.com/MetaphoricalSheep/ferumind.git
cd ferumind
just setup && just bootstrap && just verify
```

The workspace defaults to `./workspace` inside the checkout. It is your data,
so keep it somewhere else:

```bash
cp .env.example .env
echo 'FERUMIND_WORKSPACE=/home/you/ferumind-workspace' >> .env
just bootstrap
```

### 2. Create a project (optional)

You can skip this. Once connected, ask your agent to make one and it will call
`create_project`; the CLI and the tool run the same code. To do it yourself:

```bash
uv run ferumind project create notes --title "My Notes"
```

Either way you get a registry entry plus a folder skeleton with a seeded spine
and rules.

### 3. Connect ChatGPT

The main way to use Ferumind. `tunnel-client` dials out from your machine over
the [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels),
so nothing listens for inbound connections. Full MCP support in ChatGPT is
currently a [developer-mode feature](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta).

Put `CONTROL_PLANE_TUNNEL_ID` and `CONTROL_PLANE_API_KEY` in `.env`, then:

```bash
just tunnel --doctor    # validate, start nothing
just tunnel --init      # create the profile if absent, then start
just tunnel-bg          # run detached
```

[docs/tunnel.md](docs/tunnel.md) covers the variables, where secrets may live,
and stopping a background tunnel. Read the [access model](#security) first: the
relay credentials are the only thing guarding your workspace.

Claude web is not supported. It needs a public URL, which the OpenAI tunnel
does not provide, and standing one up is exactly what the gate below governs.

### 4. Point your chat project at Ferumind

One paste, once per project.
[`product/contract/bootstrap.md`](product/contract/bootstrap.md) holds the
prompt, and `just bootstrap` installs a copy to
`workspace/system/prompts/bootstrap.md`.

1. Create a project in ChatGPT.
2. Copy the text below the `---` into its instructions.
3. Replace `<PROJECT_KEY>` with your project key, for example `notes`.

Every new chat in that project then calls `get_context` before answering, and
reads the workspace rules, the spine, the document map, and the skills index
instead of starting from nothing.

### 5. Local clients (optional)

Claude Code, Claude Desktop, and Cursor run the server over stdio. Configure it
in the client, not in this repository:

```json
{
  "mcpServers": {
    "ferumind": {
      "command": "/absolute/path/to/ferumind/scripts/ferumind-mcp-stdio"
    }
  }
}
```

The path must be absolute, because clients spawn the server from their own
working directory. Point at the wrapper rather than `uv run ferumind mcp
serve`: it loads `.env` and strips control-plane credentials before starting.
Keep `FERUMIND_WORKSPACE` in `.env` so one setting covers every client. The
server appears as **Ferumind** with 48 tools.

### 6. Inspect the local runtime

Operator diagnostics stay on the machine that owns the workspace:

```bash
uv run ferumind doctor
uv run ferumind lint
uv run ferumind observations errors --since 24h
uv run ferumind dashboard --open
```

The dashboard listens only on `127.0.0.1:8765`. It is a separate, read-only
operator process rather than an MCP transport, and it continues to render a
degraded diagnostic view when the MCP server is stopped. See
[docs/observability.md](docs/observability.md) for correlation-ID lookup,
privacy guarantees, SSH forwarding, and Basecoat theme maintenance.
[`docs/lint.md`](docs/lint.md) documents the separate, report-only workspace
lint command and what each check means.

## Security

Ferumind serves one person: the owner, running it themselves. Direct SSE and
streamable HTTP transports are disabled in code.

The optional operator dashboard is a distinct loopback-only HTTP surface. It
has no mutation endpoints, is never attached to the MCP tunnel, rejects
non-local Host headers, and loads no external assets. It does not make remote
MCP serving supported.

- The MCP server does not authenticate callers. The relay and its
  `CONTROL_PLANE_*` credentials are the only access control in front of your
  workspace. Treat them as granting full read and write access to everything
  in it.
- The relay provider sits in the data path. Your document and memory payloads
  transit it.
- Account scoping and `tunnel_id` are convenience, not server-verified
  identity or workspace authorization.

Serving anyone other than yourself needs OAuth, deny-by-default
subject-to-workspace authorization, and the deployment review in
[SECURITY.md](SECURITY.md), which also holds the threat model and the reporting
process.

The public-tree release check is a necessary current-tree control, not approval
to publish by itself: it classifies tracked paths but does not inspect file
contents or Git history. Review both for secrets before making a repository
public. Live workspace content, databases, environment files, and generated
agent configurations are Git-ignored.

## Versioning

Ferumind is `0.MINOR.PATCH`. **Pin a tag** — tags mark smoke-tested commits and
never move once pushed:

```bash
git checkout v0.1.0
```

`main` moves. It is always green, but it is the integration line, not a
release.

The version number promises one thing: **a breaking change bumps the minor.**
`0.3.1` → `0.3.4` will not break you; `0.4.0` might, so read the
[changelog](CHANGELOG.md) first. Nothing enforces that for you in Python —
write `ferumind>=0.3,<0.4` yourself.

It does not promise backports, a deprecation window, a supported release
channel, or a wheel, PyPI package, or container image. The Python import API
is private and unversioned; use the MCP server or the CLI.
[docs/releases.md](docs/releases.md) has the detail, including exactly which
surfaces are covered.

## How it works

```text
Interface          MCP server · CLI · local operator dashboard
Core domain        paths · registry · documents · policy · search
                   patches · writes · snapshots · operations · diagnostics
System state       Markdown workspace · SQLite index/history · private runtime events
```

Documents get their role from their folder. Editing is lookup-first and guarded
by propose-then-apply, every mutation is snapshotted and logged, and search
runs on SQLite FTS5. Call observations and exceptional runtime events record
metadata only, never content. The CLI and local dashboard use one typed core
diagnostic/query layer rather than embedding diagnostic SQL in either UI.
Core safety logic lives in `src/ferumind/core`; the MCP and CLI layers call it
rather than duplicating it.

Edits made outside Ferumind are caught by reconcile-on-read. The next read that
touches a path stats it against the index, reindexes on drift, marks pending
proposals stale, and writes a log entry. There is no filesystem watcher.

The server is a librarian rather than an autonomous agent. The documents carry
the behavior contract; the connected chat agent exercises judgment.

## Workspace model

```text
workspace/
  system/            rules, skills, bootstrap prompt, templates, registry,
                     format marker
  projects/<key>/    spine.md, rules, canvases, memory, library, inbox, archive
  compacts/
  .ferumind/         private database, snapshots, blobs, backups
```

Run it as the OS account that owns the workspace, and never commit
`workspace/`.

`.ferumind/` grows: every edit keeps a snapshot of what it replaced, every
applied patch keeps its diff, and each `ferumind migrate` leaves a full backup
tarball. That is deliberate — snapshots are the only copies of superseded
versions — but it does not shrink on its own.

```bash
ferumind prune            # what could be reclaimed; deletes nothing
ferumind prune --apply    # actually reclaim it
```

Prune only ever touches Ferumind's own bookkeeping under `.ferumind/`. Your
documents are not part of it, `archive/` included — archiving is how you retire
a document, not how you delete one. Stop the tunnel before `--apply`; the run
rewrites the database and needs it to itself.

## Development

```bash
just verify    # format, lint, strict Pyright, tests, coverage floor
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [`AGENTS.md`](AGENTS.md),
[docs/releases.md](docs/releases.md),
[docs/python-support.md](docs/python-support.md), and
[docs/mcp-sdk-support.md](docs/mcp-sdk-support.md).

## License

[MIT](LICENSE)
