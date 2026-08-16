# Ferumind Tunnel

> **Access model:** the tunnel exposes the workspace configured by
> `FERUMIND_WORKSPACE` — including a live one — to whoever can reach the relay
> endpoint. The MCP server does not authenticate callers, so the relay and its
> credentials are the only access control. This is accepted for single-user
> development; it is not a supported production posture. See
> [`SECURITY.md`](../SECURITY.md).

The tunnel launcher exposes the Ferumind MCP stdio server through an outbound
relay so an LLM frontend can connect without an inbound port on the local
network.

## Web Chat File Compatibility

Ferumind's portable file surface uses standard MCP content:

- `ImageContent` carries a bounded JPEG or PNG rendition inline for clients
  that expose MCP image blocks to the model.
- `TextContent` carries a bounded UTF-8 slice inline.
- `ResourceLink` identifies the untouched original, and `resources/read`
  serves its exact bytes.

These are distinct promises. An inline image or text block enters the current
tool call's model context. A resource link makes the original addressable to
the MCP client; it does not force a web host to turn that resource into a
durable chat attachment.

### ChatGPT web

ChatGPT's full MCP support is currently a developer-mode feature. The standard
MCP tool result is the portable path Ferumind uses. OpenAI documents that a
tool's `content` may contain text or other MCP content, but it does not publish
a raw tool-result size limit or promise that every MCP content type is promoted
to model input by every ChatGPT surface. OpenAI separately documents optional,
host-managed [file APIs](https://developers.openai.com/plugins/reference#file-apis)
that work with ChatGPT `fileId` values; it does not document an arbitrary
`ferumind://` `ResourceLink` as a way to create such a file ID or attach a file
to the thread. Consequently, an image is returned as standard inline
`ImageContent`, but only an end-to-end ChatGPT test establishes that the host
made it visible. A PDF, Office file, archive, or other `resource_only` file must
not be described as having entered model context merely because its link was
returned.

The [ChatGPT developer-mode guide](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta)
also says approved tool definitions are snapshotted rather than updated
automatically. After changing Ferumind tool schemas or defaults, refresh/rescan
the app's actions as appropriate for the workspace, then test from a new chat
with the draft app selected. The OpenAI [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
is a development bridge for this ChatGPT path.

### Claude web

Claude explicitly supports standard text and image tool results plus text and
binary resources. It documents an approximate 150,000-character tool-result
limit for Claude.ai/Desktop and requires a remote Streamable HTTP endpoint
(legacy HTTP+SSE is being deprecated). See Claude's
[custom connector specification](https://claude.com/docs/connectors/building).

Ferumind therefore caps an encoded image rendition at 64 KiB: base64 expansion
is at most 87,384 characters, leaving substantial room for its short summary,
structured metadata, and resource link. The default is a 1024-pixel longest
edge at preferred JPEG quality 78. If needed, Ferumind first reduces quality no
lower than 70, then geometry; an explicit lower-quality request is honored.
This keeps ordinary photos useful for visual comparisons while preventing a
caller retry from defeating the result budget. For the 4.4 MB photo
used during compatibility testing, the 64 KiB budget retained a roughly
616×818 rendition.

The OpenAI tunnel should not be assumed to be a Claude connector endpoint.
Claude web needs its own Claude-reachable Streamable HTTP MCP URL and supported
authentication. Exposing Ferumind that way remains subject to the owner-only
security and deployment gate described above; do not bypass that gate by
opening the unauthenticated MCP server directly to the internet.

## How It Works

- `scripts/tunnel.sh` manages a **tunnel-client** profile for Ferumind.
- `scripts/ferumind-mcp-stdio` is a silent wrapper that loads env files and
  executes `uv run ferumind mcp serve`.
- `tunnel-client` is expected to be available on `PATH` by default.
- tunnel-client is configured to invoke the wrapper as the MCP stdio command.

## Required Environment Variables

| Variable                   | Purpose                        |
|----------------------------|--------------------------------|
| `CONTROL_PLANE_TUNNEL_ID`  | Tunnel ID from the control     |
|                            | plane provider.                |
| `CONTROL_PLANE_API_KEY`    | API key for the control plane. |

## Where Secrets Live

Secrets are loaded from every file that exists, in this order (later files
override earlier values):

1. `<repo-root>/.env`
2. `/etc/ferumind-tunnel.env`
3. `/etc/ferumind-mcp.env`

All three are optional, but tunnel startup requires both control-plane
variables after loading. Each file must be a regular, non-symlink file owned
by the current user or root and have mode `0600` or stricter.

These files are loaded by sourcing them into bash, which makes their contents
shell code rather than inert configuration. A value such as `KEY=$(command)`
executes when the launcher starts. The ownership and permission checks above
decide *who* may write a file that runs; they say nothing about what it does.
Treat editing one as editing a script, and quote values containing spaces,
`$`, backticks, or `;`.

The background launcher also writes `~/.config/tunnel-client/<profile>.log`.
The profile directory is created `0700` and the log `0600`, but relay logs can
contain the tunnel URL, which this project treats as a credential granting
full read and write access to the workspace. Do not copy that directory into a
support bundle, an issue attachment, or a backup you would share.

Never commit `.env` files. The `.env.example` file contains placeholder values
only.

Run the launcher as a dedicated, unprivileged OS user. Do not run it as root,
and do not use a checkout or workspace writable by another account.

## Usage

The tunnel serves whatever workspace `FERUMIND_WORKSPACE` resolves to, which
defaults to the repository's `workspace/`. Set that variable in `.env` if you
want the tunnel to serve a different workspace — for example a scratch one for
integration work.

Never bypass the launcher with a direct `tunnel-client run`: the launcher is
what validates the profile, records a verifiable PID, and keeps control-plane
credentials out of the MCP child's environment.

Make the scripts executable:

```bash
chmod +x scripts/tunnel.sh scripts/ferumind-mcp-stdio
```

### Initialize a profile

```bash
just tunnel --init
```

Creates a tunnel-client profile named `ferumind` (configurable via
`FERUMIND_TUNNEL_PROFILE`), then starts the tunnel. An existing profile is
kept rather than replaced; use `--force` to replace it. To validate without
starting anything, use `--doctor`.

### Replace an existing profile

```bash
just tunnel --force
```

Replaces the profile and starts the tunnel.

### Validate configuration

```bash
just tunnel --doctor
```

Runs tunnel-client doctor without starting the tunnel. It still requires both
control-plane variables to be present, and it is the one mode that is not
refused under `CI`.

### Run

```bash
just tunnel
```

Validates the profile and starts the tunnel in the foreground.

### Run in the background

```bash
just tunnel-bg
just tunnel-stop
```

`--bg` detaches the tunnel so it survives an SSH logout, writing its log to
the profile directory. The launcher records the PID together with the
process's start time and refuses to signal a PID whose identity no longer
matches, so `--stop` cannot kill an unrelated recycled PID.

## Overrides

| Variable                   | Default          |
|----------------------------|------------------|
| `FERUMIND_TUNNEL_PROFILE`  | `ferumind`       |
| `TUNNEL_CLIENT_BIN`        | `tunnel-client`  |

Examples:

```bash
FERUMIND_TUNNEL_PROFILE=ferumind-dev ./scripts/tunnel.sh --init
TUNNEL_CLIENT_BIN=/usr/local/bin/tunnel-client ./scripts/tunnel.sh --doctor
```

Every mode that starts the relay is refused whenever `CI` is set. Starting a
tunnel is an interactive, operator-initiated action.

If `tunnel-client` is already installed and on `PATH`, no extra configuration
is needed.

## Why `scripts/ferumind-mcp-stdio` Must Be Silent

The MCP stdio protocol uses **stdout** exclusively for JSON-RPC messages. Any
non-JSON output on stdout breaks the protocol — the client will try to parse
banners, status lines, or debug output as JSON and fail.

`scripts/ferumind-mcp-stdio` prints nothing to stdout. If logging is needed it
must go to stderr.

## Why tunnel-client Receives a Wrapper Path

The tunnel-client `--mcp-command` flag accepts a single executable path, not a
shell command string. Passing `scripts/ferumind-mcp-stdio` ensures:

- The wrapper is a proper executable that can be `exec`'d directly.
- It sets up environment variables before starting the MCP server.
- It uses `exec` to replace itself with the server process, avoiding extra
  process tree overhead.
