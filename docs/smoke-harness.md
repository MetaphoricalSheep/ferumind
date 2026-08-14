# The MCP stdio smoke harness

```bash
just smoke                      # the harness alone (~4s)
uv run pytest tests/smoke -q    # the same thing, directly
```

`tests/smoke/` drives a **real** Ferumind MCP server in a **real** subprocess
over a **real** stdio pipe, against a **throwaway** workspace.

## Why it exists

`tests/integration/test_mcp_surface.py` drives an in-process `Client(mcp)`. It
covers registration, schemas, dispatch, and the result envelope well, and it is
cheaper — it stays, and it remains the right place for surface-shaped
assertions. But it never crosses a process boundary, so four things stayed
unproven until this harness existed:

- stdout carries protocol and nothing else,
- the documented launcher actually launches,
- stdio framing survives real payloads (base64 uploads, multi-line diffs),
- the server starts and stops cleanly.

Before this, every ticket that touched the MCP surface ended with a hand-rolled
throwaway script proving the same four things and then deleting the evidence.
Four times in one milestone.

## The rule that matters most

> **The harness must never touch the owner's live workspace.**

This is not caution, it is a live trap with no natural alarm.
`scripts/ferumind-mcp-stdio` does:

```bash
set -a
source "$env_file"
set +a
```

and `.env` contains `FERUMIND_WORKSPACE=workspace`. **Sourcing overwrites a
variable the caller already exported.** So this, which looks obviously safe:

```bash
FERUMIND_WORKSPACE=/tmp/throwaway scripts/ferumind-mcp-stdio    # WRONG
```

silently runs the server against `<repo>/workspace` — the owner's real
documents — and a harness built that way would write documents, uploads,
archives and projects into live personal data while every assertion passed.

Measured, not assumed:

```
$ FERUMIND_WORKSPACE=/tmp/my-throwaway bash -c 'set -a; source .env; set +a; echo $FERUMIND_WORKSPACE'
workspace
```

Only the explicit flag survives, because the launcher ends in
`exec uv run ferumind mcp serve "$@"` and the flag beats the environment:

```bash
scripts/ferumind-mcp-stdio --workspace /tmp/throwaway            # the only safe form
```

### Two guards, both failing closed

Because a convention is not a guard, `tests/smoke/guard.py` enforces it twice.

**`assert_disposable_workspace(path)`** — static, in `SmokeSession.__init__`,
before a process can be spawned. The path must be an existing directory
**outside the checkout**. That is deliberately stronger than "not
`<repo>/workspace`": nothing inside the repository is disposable, and `.env`
names the live path with a relative string that only means anything relative to
the repo root.

**`assert_isolated(visible, expected)`** — dynamic, after `initialize` and
before the first write. It asks the *running server* which projects it can see,
via `list_projects`. The static check constrains what the harness **asks for**;
only this one observes what the server actually **opened**. If a future change
to the launcher, the CLI, or precedence re-routes the server at live data, the
owner's project keys appear here and the run aborts having written nothing.

Both are proven to fire in `tests/smoke/test_live_workspace_guard.py`, which
also pins the trap itself: if `.env` stops overwriting the environment, that
test fails and asks you to confirm the change was deliberate before the guard
is removed.

## Design decisions

**Through the launcher, not `ferumind mcp serve` directly.** The launcher is
what users actually run, what the tunnel runs, and what MCP clients are
configured with — so it is what breaks. It also carries the `.env` sourcing,
the `unset` of control-plane credentials, the `PYTHONSAFEPATH` hardening, and
the `cd` to the repo root. Testing the inner command instead would skip exactly
the layer where the interesting failures live, including the trap above. The
cost is that the harness inherits the trap; the guards are the answer to that,
and they are better value than avoiding it.

**It runs inside `just verify`.** The full harness is **~3.5 seconds** — one
process start amortized over ~25 calls — against a suite that takes about six
minutes. At well under one percent, excluding it would only guarantee it rots.
`testpaths = ["tests"]` picks it up with no extra wiring; `just test-no-smoke`
exists for the rare case where you want the suite without a subprocess.

**stdout purity is structural, not an assertion.** `SmokeSession` parses
**every** line it reads from stdout as JSON-RPC 2.0, with `FERUMIND_LOG_LEVEL=DEBUG`
so the noisiest configuration is the one under test. A stray `print`, or a
logging handler defaulting to stdout, fails the call that emitted it and quotes
the offending line. There is no separate check to forget to call, and the
protection strengthens automatically as the walk grows.

## The model

| Piece | Responsibility |
|---|---|
| `guard.py` | Refuse non-disposable workspaces. Knows nothing about MCP. |
| `session.py` | The only code that knows about processes, framing, and JSON-RPC. |
| `conftest.py` | The session-scoped workspace and server. |
| `test_write_domains.py` | The domain walk. Speaks `SmokeSession` and nothing lower. |
| `test_live_workspace_guard.py` | Proves the guards fire. Starts no server. |

The session is **session-scoped on purpose**. Startup costs an interpreter
launch, a `uv` resolution, and construction of the whole tool surface; a call
after that costs milliseconds. A server per test would make the harness cost
proportional to how thoroughly it checks — the exact pressure that stops people
adding checks. One process, many calls, so the walk grows almost for free.

## Adding a domain

You should not need to read `session.py` to do this.

1. Add a test to `tests/smoke/test_write_domains.py` (or a new module with
   `pytestmark = pytest.mark.smoke`) taking the `session` and `project`
   fixtures.
2. Call your tool and assert the envelope:

   ```python
   def test_my_domain_does_the_thing(session: SmokeSession, project: str) -> None:
       result = session.call("my_tool", {"project": project, "thing": "value"})
       data = result.require_ok()  # ok=True, returns data
       assert data["document_mutated"] is True
       assert result.string("path").startswith("canvases/")
   ```

3. For a failure arm, assert the machine-readable code:

   ```python
   session.call("my_tool", {"project": "nope"}).require_error("PROJECT_NOT_FOUND")
   ```

`Envelope` gives you `require_ok()`, `require_error(code)`, `string(key)`, and
the raw `ok` / `data` / `error_code` / `message` fields.

**If your test mutates a document, create your own.** An edit consumes the text
it matched, so tests sharing a document quietly become order-dependent. The
`patch_target` fixture shows the pattern; a document costs one call on a server
that is already running.

**Do not add a workspace fixture of your own.** Use `smoke_workspace`. Anything
you build yourself has to clear `assert_disposable_workspace`, and if it
doesn't, the run stops.

## Scope

This harness proves the transport and the write domains. It does **not** test
the tunnel or any relay (D11 territory), and it does not replace the in-process
surface test. If a smoke test fails because of a genuine server defect, that is
a finding — report it rather than adjusting the harness until it passes.
