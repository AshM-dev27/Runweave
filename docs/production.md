# Single-workspace E2B deployment

This profile targets one trusted workspace, at most 20 admitted runs and four held E2B computers shared by disposable jobs and warm reusable sessions. Unknown acquisition and pending cleanup retain provider capacity. These are policy ceilings, not measured throughput. Multi-tenancy and desktop automation are outside this profile.

## Configuration

Provision private PostgreSQL and self-hosted Temporal with durable storage and a dedicated namespace. Temporal uses private-network transport; Cloud/mTLS support is not implemented. Terminate HTTPS at your reverse proxy; the application port binds to host loopback. Restrict access to the single workspace API key.

Use separate PostgreSQL migration-owner and runtime roles, both with TLS. Prefer `ssl=verify-full` and trusted certificates. The runtime role needs schema USAGE, table SELECT/INSERT/UPDATE/DELETE and sequence USAGE/SELECT, including default privileges on future migrator-owned tables. It must not own the database/schema or have superuser, CREATE DATABASE, CREATE ROLE or schema CREATE privileges.

Copy [settings](../config/production.env.example) to ignored `.env.production.local` and [worker credentials](../config/worker.env.example) to ignored `.env.worker.production.local`; chmod both to 0600. Fill settings with the two database URLs, private Temporal address/namespace, a random application key of at least 32 characters and absolute worker-file path. Keep `E2B_API_KEY` and only necessary model credentials in the worker file; keep the application key out of it.

Provision `ARTIFACT_STORAGE_HOST_PATH` as a private persistent directory owned by UID/GID 10001. The API and worker mount this same directory for file bytes; PostgreSQL retains artifact metadata. Back up and restore both together. A worker on another host requires the same shared filesystem/backend, not an independent directory with the same pathname. See [artifact operations](artifacts.md). `/tmp` is a bounded 256 MiB staging mount; monitor it and persistent disk space alongside memory.

Build locked source, push to your registry, and set `RUNWEAVE_IMAGE` to its immutable `@sha256:...` reference. Retain the previous image digest and versions referenced by saved runs. Containers run as UID 10001 with a read-only root, bounded `/tmp`, no Linux capabilities and no Docker socket or host workspace mount. Local project commands require a separately secured broker; grant only provisioned capabilities.

The API is limited to one CPU and 512 MiB of memory; the worker and migrator to two CPUs and 1 GiB each. Each container permits at most 128 processes. These ceilings complement run/provider admission; observe memory and process restarts before raising workload limits.

## Release

```bash
uv run python -m scripts.production_check --env-file .env.production.local
docker compose --env-file .env.production.local -f compose.production.yaml config --quiet
docker compose --env-file .env.production.local -f compose.production.yaml up -d
```

Do not print expanded Compose configuration; it contains credentials. The checker validates files and settings without provider I/O. It cannot verify external network isolation, database grants, certificates, namespace access or backups.

Migration runs before API/worker startup. Migration `0005` adds durable capacity and cleanup indexes, accounting for existing unfinished E2B intents. Migration `0006` adds external blob references without rewriting existing inline files. Migration `0007` adds reusable computer ownership, expiry and retained admission identities. Automatic downgrade is disabled to retain evidence. Deploy matching API/worker code and registrations; retain the original E2B handler for saved text jobs. [Reusable computers](computer-sessions.md) retain E2B capacity while warm and require the v3 cleanup workflow/activity on every replacement worker.

For upgrades, close unused reusable computers, close admission, stop the API, drain or explicitly cancel active tasks, record unresolved operations, and stop the worker. Back up PostgreSQL, Temporal, artifact bytes and broker state consistently, retaining registrations and provider handles. Run migration once and start matching images. Never delete held capacity slots or repoint credentials to another provider account. Restoring persisted ownership cannot resurrect an expired computer or its unpublished guest files.

Before reopening admission, verify authenticated `/v1/readiness` and an end-to-end task. Container health checks API liveness; readiness observes database access and Temporal pollers. Neither probes provider correctness. The [invoice demo](e2b.md) is an explicit billable E2B smoke with unpaid scripted planning. Verify its files and that held capacity returns to zero.

## Monitoring and recovery

Authenticated `GET /v1/extensions/status` and `client.extension_status()` expose installed limits, held slots (`active`), terminal cleanup (`pending_cleanup`), uncertain operations (`outcome_unknown`) and the oldest slot's age (`oldest_seconds`). Alert on unknown outcomes, cleanup beyond sandbox lifetime, sustained full capacity and failed readiness/processes.

Log operation/run IDs, excluding arguments, receipts, credentials, signed URLs and provider exception bodies. Worker logging is sanitized; handler state is credential-redacted before persistence. PostgreSQL/Temporal still contain task content: restrict access and encrypt backups.

Transient polling/termination failures retry with bounded backoff. Python dispatch is never repeated once saved. Cancellation fences later phase dispatch; cleanup intent is saved before termination. Confirmed termination/expiration releases capacity. An empty metadata search is not proof of cleanup.

On a stopped run, inspect `client.recovery(run_id)` and submit the suggested `tool_cleanup` reconciliation with a stable idempotency key. It uses pinned cleanup and saved state; it cannot execute Python, create a sandbox or resume the task.

If cleanup cannot be verified automatically, independently audit the provider account and confirm the operation's resources are gone. An authorized operator can submit `kind: "tool_cleanup_attestation"`, `target_id` and required `evidence_ref` identifying that audit. This releases capacity without provider I/O. The receipt identifies attestation and `provider_cleanup_confirmed: false`; the operation remains an error and the run stays terminal. Do not attest merely to hide uncertainty or claim task success.

## Release and restore checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
TEST_PROJECT_BROKER_NAME=runweave-test-sandbox-broker \
TEST_POSTGRES_CONTAINER=runweave-tests-postgres-1 \
uv run pytest -q --integration -m integration
```

Follow [service test setup](validation-index.md#service-integration-tests) in a disposable checkout. The restore test creates a disposable database, dumps only its test schema after Python dispatch, restores/resumes the fake E2B job without relaunching Python and deletes the database/dump. It never restores over application data. CREATE/DROP DATABASE belongs only to this test/admin infrastructure.

Checks cover eight concurrent tasks under the four-job cap, worker replacement, Temporal replay, migration and restored operation identity. They do not establish hosted-provider latency, model accuracy, adversarial isolation or higher capacity. The shared workspace admission lock remains for this bounded profile.

Rehearse complete restoration using your actual PostgreSQL/Temporal backup system before admission. The application-ledger drill does not establish Temporal disaster recovery. An old backup can rewind dispatch state: keep admission closed, retain original evidence and reconcile upstream resources before resuming. Missing receipts do not prove that remote actions never occurred.
