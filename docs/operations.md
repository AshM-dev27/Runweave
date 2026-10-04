# Operations

This is a development build for one authenticated workspace. Review the [validation boundaries](validation-index.md) before deploying it beyond local development. Use the [README quickstart](../README.md) for local startup.

## Startup and recovery

`scripts.start_local` preserves `.env.local`, refuses tracked/symlink credential files, and never sources them as shell code. It creates an API-only `.env.api.local`, retains broker authentication in `.env.sandbox.local`, builds the toolkit sandbox image and uses `sudo -n` Docker when needed. Only the worker loads provider credentials.

Manual Compose startup, if needed: `sudo -n env API_PORT=18000 docker compose --profile app up -d --build`. Use ordinary Docker when your account has daemon access. `docker compose --profile app stop` retains volumes. Port 8000 may belong to another service; the documented API port is 18000.

For host development, start dependencies and migrate, then run API and worker in separate terminals:

```bash
docker compose up -d --build postgres temporal mcp
uv run python -m scripts.wait_services
uv run alembic upgrade head
uv run --env-file .env.local uvicorn agent_runtime.api:app --port 18000
uv run --env-file .env.local python -m agent_runtime.worker
```

Sandbox tasks additionally require the authenticated broker. Host workers normally use port 18090; isolated validation uses 18091. Tool discovery does not probe service health.

The worker dispatches committed outbox records; queued submissions and decisions survive worker downtime. Back up PostgreSQL, retained registrations, Temporal history and broker state together. **Do not delete volumes to recover runs.** Drain/version workflows before incompatible workflow/activity changes; migration `0002` refuses active legacy runs. Restarts do not replenish budgets.

## Upgrade and rollback

For the local Compose deployment, build `api`, `worker`, `mcp`, `migrate` and `sandbox-broker` before the cutover. Retain their previous image IDs under rollback tags. Stop API admission, drain active runs, then stop the worker and broker before backing up PostgreSQL (including Temporal databases) and the broker state volume. Keep both named volumes when recreating services.

Local rollback bundles belong in the ignored, private `var/backups/` directory. They contain the compressed PostgreSQL dump, broker state archive and `rollback.compose.json` image overrides. To restore the prior application images, use the main Compose file plus that override with `--no-build`; restore persisted state only as a separate, deliberate recovery step. Keep images referenced by run snapshots.

After an upgrade, verify authenticated readiness and execute fake smoke tasks through the running API, including approvals, event replay, contract rejection, evidence export and sandbox cancellation. Readiness alone does not exercise these paths. Retain runtime records and paid-test ledgers; remove temporary diagnostics and only unused images belonging to this project.

## Providers and configuration

[config/models.json](../config/models.json) maps public aliases to private adapters, upstream models, endpoints, authentication and limits. `MODEL_REGISTRY_FILE` selects the registry; mount the same file into API and worker and restart after changes. [config/tools.json](../config/tools.json) and [config/general.json](../config/general.json) define operator tool/project policies. Callers select installed tools; they cannot register arbitrary code or endpoints.

Registrations require explicit endpoints, authentication and tool/token capabilities. `auth: "env"` names a worker credential variable, never its value. Responses and Anthropic require it; OpenAI-compatible Chat Completions also permits `auth: "none"` without inheriting OpenAI credentials. Endpoint userinfo, queries and fragments are forbidden. Unsupported aliases/capabilities fail validation; there is no automatic fallback.

Runs pin immutable registrations. Retain referenced registrations and images; do not repoint their credential variables to different backends. Missing worker credentials yield sanitized `provider_not_configured`. Luna's supplied reasoning default is `none`. `OPENAI_FORCE_IPV4=true` uses direct IPv4 without environment proxies for adapter-created official OpenAI clients; false restores default routing. Custom endpoints and injected clients are unaffected.

## Limits and effects

Legacy `timeout_seconds` is a cumulative active budget (5–600 seconds); only approval waiting pauses it. `APPROVAL_WAIT_SECONDS` is a separate cumulative allowance (default 86400, range 1–604800), pinned per run. Exhaustion reports `run_timeout` or `approval_timeout`. Legacy model/tool activities allow two attempts, with zero SDK retries and one validation retry. Session history is capped at 250 KB; global active-run admission defaults to 20.

Toolkit children share root counters and conservative token reservations. Legacy v3 configurations (`resources: null`) default to 12 model attempts, 48 tool attempts, 16,000 tokens and 600 active seconds, with the previous operator maxima and child caps. New v3 policies use shared allocation and optional task budgets; omitted compute budgets use the pinned operator/model ceilings, with their source exposed through `/resources`. See [resource policy](resource-policy.md). Retry and ambiguous-failure reservations do not create new capacity. These are execution bounds, **not exact billing caps**.

`/budget` identifies its accounting mode: v1 reports successful recorded usage and leaves physical accounting unknown; v2 shared-ledger counters cover root and children. Inspect current contracts/policies for per-file, storage, command and output limits rather than assuming unlimited capacity.

Approved note effects and events commit atomically under stable IDs; read-only MCP calls can retry. Other mutations require upstream idempotency or reconciliation. Cancellation fences later effects but cannot erase committed ones. Approval denial persists across the applicable run tree.

## Sandbox and data security

The API and worker have no Docker socket or host mounts and drop Linux capabilities. The broker alone owns the Docker socket and private state volume: it is a trusted privileged boundary. Generated Python has no host fallback; containers share the host kernel and are not VMs.

Toolkit jobs use a fixed image, no network/host mounts, read-only root, resource limits and bounded output collection. V3 project commands use a separate Python image whose immutable ID is pinned in `config/general.json`.

For the **first setup of a fresh workspace**, build and pin that project image before starting the application:

```bash
uv run python -m scripts.prepare_project_image --update-policy
uv run python -m scripts.start_local
```

The helper builds `sandbox/Dockerfile.project` and writes the resulting image ID into the policy. It refuses to update the policy while this project's API, worker or broker is running. The startup wrapper then builds API, worker and broker with the same policy, and builds the separate toolkit image. To inspect the project image or validate that the configured image is available:

```bash
docker image inspect runweave-project:local --format '{{.Id}}'
uv run python -m scripts.prepare_project_image
```

Do not repin an existing deployment as a setup shortcut: active and resumable runs retain their original policy. Follow the upgrade procedure, retain images referenced by snapshots, and deploy compatible policies deliberately. Project commands accept bounded Python/pytest argv, not a shell API; network access and package installation are unsupported.

Keep broker/application state across restarts so interrupted jobs can reconcile. Downloads validate immutable bytes. This is one authenticated workspace, not multi-tenant ownership.

Telemetry is off by default. `OTEL_EXPORTER_OTLP_ENDPOINT` enables allowlisted metadata spans excluding prompts, responses, arguments, exception bodies and credentials. PostgreSQL and Temporal histories still contain task content and require private storage. See [testing and validation](validation-index.md) for unpaid checks and explicit live-evaluation controls.

## Installed extensions and ambiguous effects

The [harness operator guide](harness-foundations.md#tools-and-mcp) covers `EXTENSION_REGISTRY_FILE`, schema/version pinning, environment-based credentials, and deployment compatibility. Ambiguous external writes remain visible through `/v1/runs/{id}/effects` as `outcome_unknown`; they block completion and further external mutations. Use the [operator recovery API](operator-recovery.md) after obtaining upstream evidence and stopping the tree. Do not repeat the write with a new operation identity. Missing handler/context/backend versions fail closed.

New v3 action activities heartbeat every two seconds with a twelve-second timeout. External-operation leases renew every two seconds and expire after eight seconds without renewal. A replacement worker reconciles ambiguous writes through their receipts; it does not repeat an uncertain write. Old workflow histories retain their earlier timing through versioned workflow patches. Browser cleanup starts with two-, four-, eight- and sixteen-second retry delays, then thirty-second delays, for at most sixteen attempts. These are bounded recovery policies, not guaranteed cleanup latency.

Operator extension definitions can supply `approval_presentation` with a title and scalar fields drawn from `arguments`, `config`, or a previous stored `receipt`. Receipt fields require `source_capability` and exact `bindings` from current arguments to receipt keys. Presentation is snapshotted per run; unmatched facts are omitted with a warning. Referenced credentials are redacted from projected facts.

## Acceptance contracts and evidence

Caller result contracts and evidence exports are described in [result contracts and acceptance evidence](acceptance-contracts.md). Format validation runs in the server before optional semantic review, with no additional model call. Accepted evidence exports contain task content and selected source bytes; protect them as you protect the original workspace.

The project broker persists cancellation intent and serializes container creation/removal per operation. A successful cancellation cannot be followed by recreation through the same request; an already-started job is removed before cancellation is acknowledged. Cleanup or Docker availability failures remain pending. Broker startup adds the cancellation field to existing local state and reconciles interrupted operations; preserve the state volume during upgrade and restart. Use a single broker process per state volume.

The disposable [test Compose stack](../compose.test.yaml) supplies an isolated broker on port 18091 with test authentication and a private state volume. Set `TEST_PROJECT_BROKER_NAME=runweave-test-sandbox-broker` when using it; restart tests otherwise default to `agent-runtime-v3-test-broker`. Follow the [service test procedure](validation-index.md#service-integration-tests) in a disposable checkout. Never point this setting at a production broker.
