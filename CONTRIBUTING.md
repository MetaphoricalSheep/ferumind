# Contributing

Ferumind stores real user knowledge, so safety and compatibility changes need
the same care as application logic.

## Development setup

```bash
just setup
just install-hooks
just bootstrap
just verify
```

Python 3.12 or newer and `uv` are required. The locked v2 product contract is
in `product/`; where implementation and `product/spec-mcp.md` disagree, the
specification wins.

## Pull requests

- Keep core behavior in `ferumind.core`; MCP, CLI, and workers call core.
- Add deterministic success and failure tests, including adversarial tests
  for paths, files, network inputs, and concurrency where relevant.
- Do not add untyped public APIs, unjustified `Any`, broad exception
  suppression, or business logic in interface layers.
- Keep `.env`, `.env.example`, and typed configuration in sync.
- Run `just verify` and review the complete diff before requesting review.
- Keep changes focused. Explain security, migration, and compatibility impact.

Never commit live `workspace/` content, SQLite files, credentials, signed
URLs, logs containing user content, or generated agent configurations.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
