# Contributing to Runweave

Start with the [README](README.md) for setup and [PLAN.md](PLAN.md) for scope. Bug fixes, reproducible examples, tests and clearer documentation are welcome. Discuss substantial API or architecture changes in an issue before implementing them.

## Development

Use Python 3.12–3.13 and uv. From a checkout:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Default tests use fake models and need no provider keys. Service tests and the unpaid business showcase are described in [validation](docs/validation-index.md). Paid tests require explicit opt-in and their own bounded campaign; do not run the entire suite with `--live`.

## Pull requests

1. Make a focused change on a branch and explain the problem and resulting behavior.
2. Add regression coverage for changed API contracts, permissions, resource limits and recovery behavior. Use deterministic fake models by default.
3. Run the relevant checks, and describe any checks you could not run.
4. Update the usage or operations guide when behavior changes. Avoid committing generated reports, credentials or local runtime state.

Keep public schemas independent of PydanticAI and Temporal. Workflows coordinate deterministically; activities perform model and tool I/O. Submission retries, events and tool effects must remain idempotent or explicitly reconciled. Never place credentials in events or traces, and run generated code only in isolated sandboxes. [AGENTS.md](AGENTS.md) records these project conventions.

Use issues for reproducible bugs and scoped feature proposals. Include the commit, Python version, relevant configuration with secrets removed, expected behavior and a minimal reproduction. Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

Contributions are distributed under the repository's [Apache 2.0 license](LICENSE). Keep discussions respectful and focused on the work.
