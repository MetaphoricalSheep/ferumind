# Python support policy

Ferumind supports **Python 3.12, 3.13, and 3.14**. Every supported minor is
exercised in CI on every push. Nothing outside that range is supported, and the
packaging metadata refuses to install there.

## The rule

> Advertise exactly the interpreters that CI runs. Never more.

The failure this prevents is quiet: an open-ended `requires-python = ">=3.12"`
claims support for Python 3.15, 3.16, and every version after — interpreters
that did not exist when the claim was written and that no job has ever run. A
user installs on a new Python, hits a failure in a dependency or a changed
stdlib behaviour, and the project's own metadata told them it should work.

Hence the upper bound. `>=3.12,<3.15` is a statement of fact about what has been
tested, not pessimism about future Pythons.

## Where each declaration lives

Six places carry a version, and they drift independently:

| Location | Value | Purpose |
|---|---|---|
| `pyproject.toml` `requires-python` | `>=3.12,<3.15` | Installers refuse unsupported interpreters |
| `pyproject.toml` `classifiers` | 3.12, 3.13, 3.14 | Package-index metadata; informational |
| `.github/workflows/ci.yml` matrix | `["3.12","3.13","3.14"]` | The actual proof |
| `pyproject.toml` `[tool.ruff] target-version` | `py312` | Lint against the **floor** |
| `pyproject.toml` `[tool.pyright] pythonVersion` | `3.12` | Type-check against the **floor** |
| `README.md` | `3.12-3.14` | What a reader is told |

### Why the linters target the floor, not the newest

Ruff and Pyright are pinned to the **oldest** supported minor deliberately. They
are the only checks that run once rather than per-interpreter, so pointing them
at 3.14 would let 3.14-only syntax or stdlib through review, to fail at runtime
on 3.12. CI would eventually catch it, but only if a test happened to cover that
line — and the error would surface far from its cause.

Targeting the floor means the static checks reject newer-than-supported usage
before it is ever committed.

## How this stays true as the code changes

Configuration drift is not prevented by discipline; it is prevented by a test
that fails. `tests/unit/test_release_controls.py` holds four guards, and each
has been verified to fail when the thing it guards is broken:

| Guard | Fails when |
|---|---|
| `test_supported_python_range_matches_ci` | `requires-python` allows a minor the CI matrix does not run, or vice versa |
| `test_supported_python_range_matches_classifiers` | Classifiers and `requires-python` disagree |
| `test_linters_target_the_oldest_supported_python` | Ruff or Pyright targets anything but the floor |
| `test_readme_states_the_real_supported_range` | The README's stated range does not match the metadata |
| `test_no_tracked_document_claims_an_open_ended_python_range` | Any tracked Markdown says "3.12+", "3.12 or newer", or similar |

The last one exists because the README guard watched one file.
`CONTRIBUTING.md` carried a floor with no ceiling from the day the upper bound
landed, and nothing failed. Prose anywhere else should either state the exact
range or point here.

These run in `just verify`, in the pre-commit hook, in the pre-push hook, and in
CI. Changing one declaration without the others fails locally, before the commit
completes — the error message names the exact fix.

The guards derive the supported set from `requires-python` and compare
everything else against it. There is one source of truth and five things checked
against it, rather than six values someone must remember to keep aligned.

## Adding a new Python version (the 3.15 procedure)

Python 3.15 is expected around October 2026. When it arrives:

1. **Try it before promising it.** Resolve and run the suite against the new
   interpreter in a throwaway environment — do not touch `.venv`:

   ```bash
   export UV_PROJECT_ENVIRONMENT=/tmp/ferumind-py315
   uv sync --python 3.15 --all-extras --dev
   uv run --python 3.15 pytest -q
   ```

   If dependencies do not resolve, stop. That is a dependency-availability
   problem, not a Ferumind problem, and the answer is to wait. Record the
   blocking package and a date to re-check.

2. **Raise the bound and the matrix in the same commit.** Set
   `requires-python = ">=3.12,<3.16"`, add the `3.15` classifier, and add
   `"3.15"` to the CI matrix. The guards fail if you do one without the others,
   which is the point.

3. **Update the README range**, or the README guard fails.

4. **Leave the linter floor alone.** It moves only when dropping an old minor,
   never when adding a new one.

5. **Verify remotely, not just locally.** The new matrix job must be green on
   `main` before the support claim is real.

## Dropping an old Python version

The mirror image, and the only case where the linter floor moves:

1. Remove the minor from the CI matrix and the classifiers.
2. Raise `requires-python`'s lower bound.
3. Raise `[tool.ruff] target-version` and `[tool.pyright] pythonVersion` to the
   new floor. Expect new lint findings: code written for the old floor may now
   have modern equivalents Ruff will suggest.
4. Update the README.

Dropping a minor is a **breaking change for existing users**, unlike adding one.
While Ferumind is source-checkout-only alpha with no compatibility promise, the
cost is low. That changes if supported artifacts are ever published.

## Current evidence

Python 3.14.6 was verified on 2026-08-06 before the support claim was made:
dependencies resolved with no conflicts, and all 763 tests passed. Re-verified
on 3.14.6 on 2026-08-14: 1,495 tests passed at 91.27% coverage.

Those counts are dated records, not live claims — the suite grows. Re-run
rather than trusting the number.
