set shell := ["bash", "-cu"]

default:
    @just --list

# Install all project dependencies.
setup:
    uv sync --all-extras --dev

# Install repository git hooks.
install-hooks:
    scripts/install-hooks.sh

# Format the entire repository.
format:
    uv run ruff format .

# Check formatting without modifying files.
format-check:
    uv run ruff format --check .

# Run Ruff lint checks.
lint:
    uv run ruff check .

# Run static type checking.
typecheck:
    uv run pyright

# Run the full test suite.
test:
    uv run pytest

# Run tests with coverage reporting.
test-cov:
    uv run pytest --cov=src/ferumind --cov-report=term-missing

# Run the full verification pipeline.
verify:
    scripts/verify.sh

# Initialize the local workspace.
bootstrap *args='':
    uv run python scripts/bootstrap_workspace.py {{args}}

# Regenerate all agent configs, overwriting generated files.
sync-agents:
    uv run python scripts/sync_agent_configs.py --force

# Regenerate a specific agent target: just sync-agent-target cursor
sync-agent-target target:
    uv run python scripts/sync_agent_configs.py --force --target {{target}}

# Run the Ferumind CLI with optional arguments.
cli *args='':
    uv run ferumind {{args}}

# Reseal a hand-edited workspace compact after deliberate edits.
compact-reseal token:
    uv run ferumind compact reseal {{token}}

# List every project (registry, folder, database), deduplicated but tagged by source.
project-list *args='':
    uv run ferumind project list {{args}}

# Clean stale registry/database state after a project folder has already been removed.
project-delete key *args='':
    uv run ferumind project delete {{key}} {{args}}

# Start the tunnel helper with optional arguments.
tunnel *args='':
    ./scripts/tunnel.sh {{args}}

# Start the tunnel in the background (detached, survives SSH logout).
tunnel-bg:
    ./scripts/tunnel.sh --bg

# Stop the background tunnel.
tunnel-stop:
    ./scripts/tunnel.sh --stop

debug-env:
    @echo "PWD=$PWD"
    @echo "TUNNEL_CLIENT_BIN=${TUNNEL_CLIENT_BIN-<unset>}"
    @echo "PATH=$PATH"
    @command -v tunnel-client || true
