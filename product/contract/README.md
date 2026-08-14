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
| `skills/distilling-durable-knowledge.md` | `workspace/system/skills/distilling-durable-knowledge.md` |

`workspace/AGENTS.md` is generated from `rules/` (short pointer, not a copy).

Editing discipline for these files: they are loaded into every chat on every
project (`get_context` concatenates them), so brevity is a feature. Payload
telemetry (spec-mcp §4) reports what they cost per call.

**`skills/` is the exception, and the discipline is different.** A skill's body
is *not* loaded into every chat: `get_context` carries only its `name` and
one-line trigger `description`, and `read_skill` fetches the body when the
trigger matches. So the body may be as long as the procedure genuinely needs,
while the **trigger** pays the per-call cost and must stay one line. A skill
whose trigger cannot be stated in one sentence is probably two skills.

Every skill needs `name` (matching its filename stem, lowercase and
hyphen-separated) and a `description` starting with "Use " that says *when* to
reach for it. Both are enforced by test, not convention.
