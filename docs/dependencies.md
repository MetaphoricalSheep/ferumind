# Dependency update policy

Ferumind pins GitHub Actions to full commit SHAs and locks Python dependencies
in `uv.lock`. That stops silent retagging; it does not keep pins current.
Dependabot opens weekly proposals; this document is how those proposals are
handled so the queue stays small enough to review.

## Tiers

| Ecosystem | Update | Handling |
|---|---|---|
| GitHub Actions | patch / minor | Auto-merge once `ci-gate` and `analyze` pass |
| GitHub Actions | major | Manual — verify the SHA against the publisher, read release notes, confirm no relied-on input was dropped (a removed input is ignored by Actions, not rejected) |
| Python (`uv`) / npm (`.opencode`) | patch | Auto-merge once required checks pass |
| Python / npm | minor / major | Manual |
| `mcp` (any level) | — | **Always manual.** `src/ferumind/mcp/sdk_internals.py` attaches to two SDK surfaces with no public equivalent; an automated bump can break startup. See [docs/mcp-sdk-support.md](mcp-sdk-support.md). |

Auto-merge is GitHub's native pull-request auto-merge, enabled by
`.github/workflows/dependabot-auto-merge.yml` for Dependabot PRs in the auto
tiers only. The workflow does **not** approve PRs. Required status checks on
`main` still gate every merge.

## Aging

Dependabot's platform default cooldown is **three days** for version updates
(security updates still open immediately). That is intentional and must stay:
a compromised upstream that publishes a plausible release is the one threat
SHA pinning cannot address, and time is the cheapest defence. Do not set
`cooldown.default-days: 0` in `.github/dependabot.yml`.

## Grouping

`.github/dependabot.yml` groups patch (and Actions non-major) updates so one
PR covers several bumps. Majors and `mcp` are excluded from groups so they
arrive as individual, human-reviewed PRs.

## What a human still does

For every **manual** PR:

1. Read the upstream release notes.
2. For Actions majors: confirm the commit SHA on the publisher's repository,
   and that every input this repo relies on still exists (a removed input is
   ignored by Actions, not rejected).
3. For `mcp`: run the MCP matrix / smoke you would run for a range change —
   green Dependabot CI proves resolve, not behaviour.
4. Merge via squash when satisfied.

If the open Dependabot PR count is growing week over week, the policy has
failed — shorten review latency or tighten groups, do not bulk-merge.
