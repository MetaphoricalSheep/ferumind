# Command: bootstrap-agents

**Purpose:** Regenerate local agent configuration files from the committed OpenCode source.

## Steps

1. Run the sync script with force:
   ```bash
   uv run python scripts/sync_agent_configs.py --force
   ```
2. Verify generated files exist:
   - `.cursor/rules/`
   - `CLAUDE.md`, `.claude/skills/`, `.claude/agents/`
   - `.github/copilot-instructions.md` and `.github/instructions/`
   - `.codex/README.md`

The source is `AGENTS.md` plus `.opencode/`. Everything above is Git-ignored
and generated; edit the source, never the output.
