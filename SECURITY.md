# Security Policy

## Current deployment status

Ferumind 0.1 is a single-user, local-first system. The supported server
transport is local stdio. Direct SSE and streamable HTTP transports fail
closed in code.

`ferumind dashboard` is not a server transport. It is a separate read-only
operator process bound unconditionally to `127.0.0.1`, with a localhost Host
allowlist, self-only content policy, explicit static-asset routes, and no CORS
or mutation endpoints. It is never connected to the MCP tunnel. An operator
administering another machine should use SSH local port forwarding; Ferumind
does not provide a public-bind option.

The public-tree check is necessary but not sufficient for publishing: it
checks current tracked path names and workflow pins, not file contents or Git
history. Review both the current content and the complete history before
making an existing repository public. Revoke any exposed credential and
rewrite the affected history or publish from a reviewed clean export. Do not
publish a checkout containing a live workspace, and do not expose the
workspace or MCP server on a public network yet. Server-verified OAuth for
ChatGPT and Claude, an authorization policy bound to one workspace owner, and
the associated deployment controls must ship before remote serving is
considered supported.

The tunnel scripts serve the configured workspace — including a live one —
over an outbound relay to a single connected client. This is accepted for
single-user development on a private, non-public deployment. It is not a
supported production posture: the MCP server does not authenticate callers, so
the relay and its credentials are the only access control in front of the
workspace. An outbound relay removes an inbound listening port; it does not
replace server-verified authentication and authorization.

Treat the tunnel URL and `CONTROL_PLANE_*` credentials as secrets granting
full read and write access to the workspace. Rotate them if they are exposed
in logs, screenshots, or connector configuration. The launcher refuses to
start from CI. Server-verified OAuth for ChatGPT and Claude, plus an
authorization policy bound to one workspace owner, must ship before remote
serving is considered supported for anyone other than the workspace owner
running it themselves.

## Supported versions

This project is pre-1.0. Security fixes are made on the latest `main` branch
and reach users in the next tag.

A tag is a snapshot, not a maintained line. No older tag receives backports:
if `0.4.0` is out, a fix for a problem present in `0.3.x` ships as `0.4.x` or
later, and upgrading is the remedy. Only the newest release is supported.
[docs/releases.md](docs/releases.md) sets out the versioning scheme and what
it does and does not promise.

## CI supply-chain policy

Third-party GitHub Actions must be pinned to a full Git commit SHA, with the
corresponding release version retained in a comment for readability. Container
actions referenced with `docker://` must use a full `sha256` image digest.
Movable action or image tags such as `@v6` and `:latest` are not accepted
because they can begin executing different code without any change to this
repository.

Dependabot checks GitHub Actions weekly. An update must remain an explicit,
reviewable repository change, and the proposed SHA must be verified against
the action publisher's official release before merging. Workflow permissions
stay least-privileged, and checkout credentials are not persisted unless a
specific reviewed job requires them.

The verification pipeline enforces full-SHA action references, exact
release-version comments, and full Docker image digests. It also rejects
tracked paths associated with workspace content, environment files,
credentials, runtime databases, generated agent configurations, and installed
dependency trees. These filename controls do not replace content and history
review.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature under the
repository's **Security** tab. Do not open a public issue for a suspected
vulnerability and do not include real workspace content, credentials, signed
download URLs, or other personal data in a report.

Include:

- the affected commit or version;
- a minimal reproduction using synthetic data;
- impact and realistic attack preconditions;
- any suggested mitigation.

## Security boundaries

Ferumind protects against accidental or malicious MCP inputs crossing a
configured workspace/project boundary. Relevant controls include:

- canonical contained paths and refusal of symlinked descendants;
- strict project keys and folder-derived document roles;
- hash-guarded proposal/apply edits;
- snapshot-before-mutation and operation logging;
- no MCP hard-delete surface;
- bounded uploads, regex execution, searches, and mutation sizes;
- HTTPS-only, DNS-pinned remote file fetches with redirect revalidation,
  public-address enforcement, response-encoding checks, timeouts, and byte
  limits;
- private workspace/database/snapshot/secret permissions;
- metadata-only MCP observations, private metadata-only runtime diagnostics,
  and generic correlated internal errors;
- a loopback-only, read-only operator dashboard with no external assets;
- disabled direct network transports.

The following are outside the current trust boundary:

- an attacker who can write to the workspace or repository as the operating
  system account running Ferumind;
- hostile multi-user sharing of one workspace;
- arbitrary third-party agent/plugin code running as that account;
- instructions embedded in content the operator did not write, acted on by a
  connected agent (see below);
- compromise of the host, Python runtime, dependency registry, relay, or LLM
  provider account.

Project names are assertions, not per-project authorization capabilities.
OAuth alone will therefore not make a multi-tenant Ferumind deployment safe.

## Untrusted content and agent instructions

Ferumind's documents carry the behavior; the server is a librarian. It
protects, indexes, and assembles — it does not decide what an agent does.
`edit_policy` reflects that: a proposal result echoes the target's policy and
a note, and the server does not block on it. The closed list of hard refusals
is archived targets, protected frontmatter identity keys, and out-of-project
paths. Policy is not on that list.

One consequence is worth stating plainly. A connected agent may create
documents under `rules/`, and `get_context` concatenates every `rules/*.md`
into the behavior text handed to that project's future sessions. So text that
reaches an agent's context — an uploaded PDF, a fetched page, a document
someone sent you, a search snippet — can induce a write whose instructions
outlive the conversation that planted them.

That persistence is the part specific to Ferumind. An agent acting on hostile
text can already write anywhere in the project and send anything back over the
same connection, within the turn it was given; a durable workspace lets the
instruction survive the chat. Both are properties of handing an agent write
access, not defects in the containment, hash, snapshot, and logging controls
above, which apply to every write regardless of what motivated it.

In practice:

- Treat writes to `rules/` as privileged, and review them the way you would
  review a change to a configuration file. `list_snapshots` and the operation
  log show what changed and when.
- `get_context` labels every rule with its source path
  (`## projects/<key>/rules/<file>`), so an agent and a reader can both tell a
  workspace rule from one added to a single project.
- Content you did not write is untrusted input to your agent. It is not
  untrusted input to Ferumind's own path, hash, and boundary checks, which do
  not depend on agent cooperation.

The server does not enforce `edit_policy` today, and this is a deliberate
product decision rather than an oversight. Turning the policies into enforced
mutation rules is tracked as an internet-exposure gate item, not a
source-release one.

## Required gate before internet exposure

Remote support is not complete until all of these are implemented and tested:

1. OAuth authorization-code flow with PKCE, strict redirect URI matching,
   state/nonce validation, short-lived tokens, rotation/revocation, and
   server-side issuer/audience/signature validation.
2. Authorization that binds the authenticated subject to exactly one
   configured workspace and denies unknown subjects by default.
3. TLS at the only ingress, no direct application port exposure, and trusted
   proxy configuration with an explicit host/origin policy.
4. Request-body, response, rate, concurrency, upload, and wall-clock limits at
   the ingress. `get_context` is intentionally uncapped by the product
   contract, so the gateway and workspace sizing policy must account for it.
   The server refuses a read it cannot deliver over the configured transport
   ceiling (`RESPONSE_TOO_LARGE`), but that is a transport guard, not a
   capacity budget: it bounds one response, not concurrent or aggregate load.
5. A dedicated unprivileged service account, a non-group-writable checkout,
   workspace directories at `0700`, secret files at `0600`, and encrypted,
   access-controlled backups.
6. Egress policy permitting only required HTTPS destinations. Cloud metadata,
   loopback, private, link-local, special-use, and non-443 destinations must
   remain denied.
7. Central security logging and alerting that never records document bodies,
   patch bodies, authorization headers, cookies, tokens, or signed URLs.
8. Restore drills, whole-workspace storage quotas/retention, dependency and
   secret scanning, and an incident-response/revocation procedure.
9. Crash-recovery testing for filesystem-plus-SQLite mutations (including
   forced process termination), with deterministic reconciliation or a
   durable intent journal for every partially published state.
10. Independent review of the final authentication, authorization, proxy, and
   deployment changes.

Until that gate passes, the correct internet deployment decision is **no-go**.
