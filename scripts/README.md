# Development scripts

Run scripts as modules from the repository root with `uv run python -m scripts.<name>`.

| Entry point | Purpose |
| --- | --- |
| `start_local` | Start the local application with private credentials and persistent volumes. |
| `prepare_project_image` | Validate the pinned sandbox image; explicitly build and pin one for a fresh installation with `--update-policy`. |
| `wait_services` | Wait for PostgreSQL, Temporal and the example MCP server. |
| `showcase_benchmark` | Unpaid public-API showcase with generated business data and simulated upstream services. |
| `e2b_demo` | Explicitly billable isolated E2B smoke with scripted planning; preserves uncertain state for recovery. |
| `computer_benchmark` | Explicitly billable E2B file, continuation, recovery and limit checks against an idle deployed workspace; optional local worker replacement. |
| `production_check` | Validate the single-workspace deployment settings and private file permissions without provider I/O. |

See [validation](../docs/validation-index.md) for prerequisites and supported commands.

The remaining modules support regression tests and historical evaluation harnesses. Some include fixed campaign identities and paid-call guards; they are not general quickstarts. Default tests use their fixtures and mocks without making paid requests. Historical campaigns must not be restarted or given new capacity by moving or resetting their ledgers. Create a separate, explicitly bounded campaign for any new paid evaluation.

Generated reports, downloads, histories and ledgers are local artifacts and are excluded from Git. Small synthetic regression fixtures remain versioned in `tests/fixtures/` and `docs/fixtures/`.
