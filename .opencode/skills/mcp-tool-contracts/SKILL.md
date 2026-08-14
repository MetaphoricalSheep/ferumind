---
name: mcp-tool-contracts
description: Use when adding or changing an MCP tool's result shape, output schema, or description — what a tool promises a caller and how that promise is enforced.
compatibility: opencode, claude-code, cursor, copilot, codex
metadata:
  project: ferumind
  role: mcp-surface
---

# Skill: MCP Tool Contracts

Companion to `mcp-hardening`, not a replacement. Hardening answers *"can this
leak or escape?"*. This answers *"what does this tool promise, and where is
that promise written down?"* Touch a tool's result and you need both.

## The rule this skill exists to prevent breaking

**A claim about SDK behaviour is only true if a test runs it.**

Ferumind carried a docstring asserting that registering without
`structured_output=False` made the SDK "raise on every real client call",
annotated *"re-verified against mcp 2.0.0"*. It was never re-verified. It had
been true on mcp 1.x; on 2.x the SDK short-circuits a bare `CallToolResult`
return to "no schema" by itself, and the flag was inert on all 46 tools. A
test asserted the wrong invariant, so a whole protocol feature stayed switched
off across a major version bump.

So: when you write down what the SDK does, either pin it with a test that
would fail on the next SDK bump, or don't write it down. Do not write
"verified against version X" — write the test.

## Division of labour: schema vs description

The output schema and the description are not two places to say the same
thing. Duplication is how they drift — `list_projects` advertised a `path`
field its code never returned, because prose was the only contract.

- **The schema says what comes back.** Field names, types, optionality. It is
  the machine contract, validated on every call.
- **The description says when to call it, and what the result *means*.**
  Ordering ("prefer `search_project` → `read_document_range` over
  `read_document`; call `get_document_map` only when structure is still
  needed"), semantics a type cannot carry ("a propose result is not a saved
  edit"), and consequences.

When you add a schema, delete the field enumeration from the description —
never both. Keep the sentence that tells a model *why* it would call this.

Guidance identical across a tool family belongs in the server `INSTRUCTIONS`
string, which ships once, not in eight descriptions that ship every time.
`_IMAGE_NORMALIZATION_NOTE` in `mcp/upload_tools.py` is the existing precedent.

## The envelope is the output schema — both arms

Every tool returns the Ferumind envelope (`spec-mcp` §5: "existing
structured-result pattern stays"). Tools declare it as:

```python
-> Annotated[CallToolResult, FerumindResult[SomePayload]]
```

The SDK derives `outputSchema` from the annotation's metadata type and then
passes the hand-built `CallToolResult` through **verbatim** — content blocks,
`is_error`, `ImageContent` and all. That is why tools can keep returning the
transport type and still advertise a schema.

**The schema must accept the error arm.** The SDK validates
`structured_content` on *every* result, including `is_error=True`. Four paths
must validate against the same model:

1. success — `make_success`
2. domain error — `make_error`
3. sanitised crash — `INTERNAL_ERROR` from `tool_boundary`
4. rejected arguments — `VALIDATION_ERROR` from `tool_boundary`

A payload model that makes `data` required, or forbids `error_code`, raises
`ToolError` on every failure — and that exception's text quotes the rejected
input, which is exactly the leak `tool_boundary` exists to stop. `ok` is the
discriminator: true means read `data`, false means read `error_code`.

## Payload model rules

- **Never put `JsonValue` in a payload model.** `core/types.py` defines it
  recursively. It makes schemas unresolvable and bloats them. For genuinely
  free-form maps (frontmatter, sidecar metadata) declare a one-level
  `dict[str, str | int | float | bool | None]`.
- **Keep `$defs`; never inline them.** Inlining `get_context` measured
  3.5 KB → 30 KB and still left a `$ref` behind. Nested models are fine.
- **Root must stay `type: object`.** No root-level `$ref` or `anyOf` — several
  clients reject those outright. A flat envelope with a typed `data` keeps the
  root an object; a tagged `Literal[True]/Literal[False]` union does not. This
  is why the envelope is flat.
- Payload models live in `mcp/result_models.py`, are `extra="forbid"`, and
  describe **only** what the tool actually returns. If the core already has a
  model for it, reuse that rather than restating it.

## Schema richness is tiered, on purpose

Tool definitions are context the model pays for on every session. Richness
follows planning value, not uniformity:

- **Rich** — tools whose result a model reasons about before its next call:
  the reads, `list_files`/`read_file`, the `propose_*` family, `apply_patch`.
  Full field-level typing.
- **Thin** — bookkeeping whose result is a confirmation: `rebuild_index`,
  `discard_patch`, `discard_upload`, the chunk appenders. A few scalars.

Reusing one payload model across a family (all eight `propose_*` share
`ProposalData`) saves maintenance, not bytes — Pydantic re-emits `$defs` per
schema. Budget accordingly.

## Before you call it done

Not a checklist to eyeball — commands to run:

```
uv run pytest tests/integration/test_mcp_surface.py tests/unit/test_mcp_hardening.py
just verify
```

`TestWireLevelConversion` is where the contract lives, because a direct
`tool.fn` call never reaches `convert_result` and so cannot see any of it:

| Guard | Fails when |
|---|---|
| `test_every_tool_advertises_a_client_usable_output_schema` | a schema is missing, its root is not `type: object`, or it reaches `JsonValue` |
| `test_all_four_result_paths_validate_for_every_tool` | a payload model rejects an error arm |
| `test_no_tool_passes_structured_output_false` | someone passes the flag that strips schemas |
| `test_declared_and_constructed_envelopes_cannot_drift` | `FerumindResult` and `FerumindToolEnvelope` disagree |
| `test_descriptions_do_not_restate_the_schema` | prose re-enumerates result fields |
| `test_tool_definitions_stay_inside_their_context_budget` | `tools/list` grows past its ceiling |

Success payloads are covered separately and everywhere: the `call()` helper in
that file validates each result against the tool's own output model, so every
assertion in the suite checks the contract. That guard exists because it was
missed once — a payload model claimed a `document_mutated` field the ChatGPT
upload tools never returned, the whole suite stayed green, and only a
wire-level test caught it.

These enumerate the live surface, so a new tool needs no edits here. If one
fails, the surface changed: fix the tool, not the test. Raising the budget is
allowed but must happen in the same commit, with a reason.
