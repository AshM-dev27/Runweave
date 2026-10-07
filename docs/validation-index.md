# Testing and validation

Runweave separates runtime correctness from model accuracy. Fake-model tests check contracts, policy enforcement and recovery. Live evaluations check whether a selected model produces the right result for a specific task. Neither alone establishes production readiness.

## Default checks: no provider calls

From a checkout with Python 3.12 or 3.13 and `uv` installed:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The default suite uses fake models and temporary databases. Integration and live tests are skipped unless explicitly enabled. No provider key is required; the test fixture disables model requests and removes Browser Use and E2B credentials for tests without the `live` marker. CI runs these same checks.

## Service integration tests

Reusable-computer checks live in `tests/test_computer_sessions.py` and `tests/test_computer_sessions_integration.py`. They cover conversation ownership, exclusive use, continuation across tasks, retained shared capacity, lost acquisition/command/completion responses, cancellation, explicit close, idle expiry after worker replacement and workflow replay. Models and computers are fake. Warm reuse does not imply a persistent interpreter, pause/resume, desktop control, measured throughput or live-model correctness.

E2B credentials are also removed by default tests. E2B service tests cover concurrent admission, worker replacement, workflow replay and a disposable PostgreSQL upgrade/restore drill. Set `TEST_POSTGRES_CONTAINER` to the disposable container name (`runweave-tests-postgres-1` in CI). See the [production profile](production.md) for what these checks establish and the deployment-specific checks still required.

File execution checks live in `tests/test_artifact_files.py`, `tests/test_e2b_artifacts.py` and `tests/test_e2b_artifacts_integration.py`. They cover larger/binary transfers, immutable blob storage, format/size admission, missing/corrupt storage, local upload snapshots on retries, run-scoped reads, duplicate-publication recovery, partial outputs, cancellation fencing, shared admission across the two E2B schemas, replacement workers and Temporal replay. Computer outputs and model decisions are fake; these checks do not establish PDF/XLSX parser availability, live-model business accuracy, hosted-provider throughput or shared-filesystem disaster recovery.

Use a **disposable checkout** and the isolated [test Compose stack](../compose.test.yaml). The [CI workflow](../.github/workflows/ci.yml) contains the complete build, image-pinning, startup, test and cleanup commands. It builds the toolkit and project images, sets the checkout's project-image digest, then starts PostgreSQL, Temporal, MCP and the test sandbox broker.

Ports 5432, 7233, 8001 and 18091 must be free. Do not run the test stack alongside the application stack on those ports. Its PostgreSQL and broker volumes are separate from application volumes; the cleanup command removes the test stack's data. Changing the project-image digest is intended only for that disposable checkout, not an existing deployment with resumable runs.

Once the test services are ready, run:

```bash
TEST_PROJECT_BROKER_NAME=runweave-test-sandbox-broker \
TEST_POSTGRES_CONTAINER=runweave-tests-postgres-1 \
uv run pytest -q --integration -m integration
```

Tests use dedicated PostgreSQL schemas and Temporal queues. `TEST_DATABASE_URL` can select a separate PostgreSQL instance; the test account needs permission to create and drop schemas. Temporal is expected at `localhost:7233` and MCP at `localhost:8001`. The broker listens on port 18091 with the test credential supplied by the test Compose file. Some Docker tests invoke `sudo -n docker`.

Run broker-dependent tests sequentially. Tests deliberately restart workers and the test broker, cancel operations, and remove their own schemas and containers. Never point `TEST_PROJECT_BROKER_NAME` at a production service. `--integration` alone does not enable paid model calls.

## Reproducible business scenarios

The [business showcase harness](../scripts/showcase_benchmark.py) creates dummy customer CSVs, orders and supplier pages, then exercises a separate HTTP API and worker against local PostgreSQL and Temporal. With those two services running on the default local ports:

```bash
uv run python -m scripts.showcase_benchmark
```

It tests CRM import after a worker crash, approval denial, refund response loss, ineligible refunds, unknown-receipt recovery, supplier comparison and browser cancellation. The runtime, HTTP transport, persisted approvals, reconciliation and workflow replay are real. Agent decisions and upstream business services are deterministic simulations; Browser Use requests go to a local simulated service. The harness does not load dotenv files or call paid providers.

Reports and generated fixtures are written under the ignored `var/benchmarks/` directory. The harness removes temporary services, database schemas and diagnostic logs. Its results measure these recovery scenarios, not live-model accuracy, hosted-browser latency or broad resistance to prompt injection.

The [reusable-computer benchmark](computer-sessions.md#deployment-and-validation) explicitly opts into billable E2B compute with `--live`. Against an idle deployed workspace it checks synthetic multi-file reconciliation, continuation, payment replay, binary outputs, checkpoint recovery, quota/timeout failures and warm-computer admission. Planning is scripted and outputs are independently checked. `--restart-worker` additionally replaces the local Compose worker between tasks. It closes only benchmark-owned computers and stores private evidence under `var/benchmarks/`; it does not establish autonomous model accuracy or sustained capacity.

## Live evaluation: explicit opt-in

Live checks require your own worker credentials, a registered model, and an explicit decision to incur provider charges. Never add `--live` to an entire test suite: some files retain one-shot historical campaign definitions. Select the intended scenario and inspect its budget and provider configuration before running it.

For each new evaluation, define:

- The task inputs, expected outputs and grading rules before execution.
- A fresh campaign identity, immutable request ledger, request limits and output-token limits.
- The model alias, reasoning settings, source revision and tool configuration.
- Exact task success, completion rate, field-level accuracy, usage and latency as separate measurements.

A schema-valid answer can still contain the wrong business decision. Grade failed and unfinished tasks, retain failures, and do not weaken expected results after seeing outputs. Small synthetic samples are useful regression references, not general accuracy estimates. Token and request bounds are execution controls; they are not exact currency billing caps.

### Campaign restrictions

Completed campaigns remain terminal. Do not reset their ledgers, reopen cells, reuse remaining allowances or relocate a ledger to extend a cap. Preserve the original local evidence and provider accounting. New evaluation work requires a fresh bounded campaign.

## Evidence and reporting

[Result contracts and acceptance evidence](acceptance-contracts.md) document deterministic final-answer checks and offline verification. Evidence hashes establish internal consistency; they do not prove authenticated origin or semantic correctness.

Keep generated reports, workflow histories, downloads and backups out of Git. They may contain task content or operational details. Publish benchmark summaries only with reviewed fixtures, methods, limitations and appropriately sanitized evidence. Routine implementation and test results belong in the pull request or CI output rather than dated repository reports.
