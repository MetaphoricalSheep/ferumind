# Contract content (source of record)

These files are the actual text the MCP server delivers and the templates
`create_project` seeds from. They are reviewed here, then installed into the
live workspace by `scripts/bootstrap_workspace.py`:

| Here | Installs to |
|---|---|
| `rules/00-contract.md` | `workspace/system/rules/00-contract.md` |
| `rules/10-editing.md` | `workspace/system/rules/10-editing.md` |
| `rules/20-memory.md` | `workspace/system/rules/20-memory.md` |
| `rules/30-reminders.md` | `workspace/system/rules/30-reminders.md` |
| `bootstrap.md` | `workspace/system/prompts/bootstrap.md` |
| `templates/spine.md` | `workspace/system/templates/spine.md` |
| `templates/project-rules.md` | `workspace/system/templates/project-rules.md` |

`workspace/AGENTS.md` is generated from `rules/` (short pointer, not a copy).

Editing discipline for these files: they are loaded into every chat on every
project (`get_context` concatenates them), so brevity is a feature. Payload
telemetry (spec-mcp §4) reports what they cost per call.
