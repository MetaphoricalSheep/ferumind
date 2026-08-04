# Agent Configuration Strategy

## Source of Truth

**OpenCode** is the committed source of truth for agent instructions.

- `AGENTS.md` — project-level agent instructions
- `.opencode/commands/` — reusable command definitions
- `.opencode/skills/*/SKILL.md` — domain-specific skill definitions

## Generated Configs

Other agent configs are generated from the OpenCode source and are Git-ignored:

| Agent | Generated Files |
|-------|----------------|
| Cursor | `.cursor/rules/*.mdc` |
| Claude Code | `CLAUDE.md`, `.claude/skills/*/SKILL.md` |
| Copilot | `.github/copilot-instructions.md`, `.github/instructions/*.md` |
| Codex | `.codex/README.md` |

## How to Sync

```bash
# Regenerate all agent configs
just sync-agents

# Regenerate specific agents
just sync-agent-target cursor
just sync-agent-target claude
just sync-agent-target copilot
just sync-agent-target codex
```

Without `--force`, existing generated files are not overwritten.

Underlying commands if you are not using `just`:

```bash
uv run python scripts/sync_agent_configs.py --force
uv run python scripts/sync_agent_configs.py --force --target cursor
uv run python scripts/sync_agent_configs.py --force --target claude
uv run python scripts/sync_agent_configs.py --force --target copilot
uv run python scripts/sync_agent_configs.py --force --target codex
```

## Important

**Do not edit generated files directly.** If you need to change agent instructions, edit the source files (`AGENTS.md` or `.opencode/`) and re-run the sync script.
