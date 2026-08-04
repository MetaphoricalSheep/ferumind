# Command: bootstrap-agents

**Purpose:** Regenerate local agent configuration files from the committed OpenCode source.

## Steps

1. Run the sync script with force:
   ```bash
   uv run python scripts/sync_agent_configs.py --force
   ```
2. Verify generated files exist:
   - `.cursor/rules/`
   - `CLAUDE.md` and `.claude/skills/`
   - `.github/copilot-instructions.md` and `.github/instructions/`
   - `.codex/README.md`

## Summary

The script generates equivalent local agent configs from the OpenCode source (AGENTS.md + .opencode/). The generated files are Git-ignored and must not be edited directly.
