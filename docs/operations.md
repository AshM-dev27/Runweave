# Operations

This is a development build; [latest acceptance](validation-index.md) does not justify production rollout. Use the [README quickstart](../README.md) for local startup.

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

## Providers and configuration

[config/models.json](../config/models.json) maps public aliases to private adapters, upstream models, endpoints, authentication and limits. `MODEL_REGISTRY_FILE` selects the registry; mount the same file into API and worker and restart after changes. [config/tools.json](../config/tools.json) and [config/general.json](../config/general.json) define operator tool/project policies. Callers select installed tools; they cannot register arbitrary code or endpoints.

Registrations require explicit endpoints, authentication and tool/token capabilities. `auth: "env"` names a worker credential variable, never its value. Responses and Anthropic require it; OpenAI-compatible Chat Completions also permits `auth: "none"` without inheriting OpenAI credentials. Endpoint userinfo, queries and fragments are forbidden. Unsupported aliases/capabilities fail validation; there is no automatic fallback.

Runs pin immutable registrations. Retain referenced registrations and images; do not repoint their credential variables to different backends. Missing worker credentials yield sanitized `provider_not_configured`. Luna's supplied reasoning default is `none`. `OPENAI_FORCE_IPV4=true` uses direct IPv4 without environment proxies for adapter-created official OpenAI clients; false restores default routing. Custom endpoints and injected clients are unaffected.

## Limits and effects

Legacy `timeout_seconds` is a cumulative active budget (5–600 seconds); only approval waiting pauses it. `APPROVAL_WAIT_SECONDS` is a separate cumulative allowance (default 86400, range 1–604800), pinned per run. Exhaustion reports `run_timeout` or `approval_timeout`. Legacy model/tool activities allow two attempts, with zero SDK retries and one validation retry. Session history is capped at 250 KB; global active-run admission defaults to 20.

Toolkit children share root counters and conservative token reservations. V3 defaults are 12 model attempts, 48 tool attempts, 16,000 tokens and 600 active seconds; operator maxima are 24/96 attempts, the registration token ceiling and 1,800 seconds. Children have smaller local ceilings. Retry and ambiguous-failure reservations do not create new capacity. These are execution bounds, **not exact billing caps**.

`/budget` identifies its accounting mode: v1 reports successful recorded usage and leaves physical accounting unknown; v2 shared-ledger counters cover root and children. Inspect current contracts/policies for per-file, storage, command and output limits rather than assuming unlimited capacity.

Approved note effects and events commit atomically under stable IDs; read-only MCP calls can retry. Other mutations require upstream idempotency or reconciliation. Cancellation fences later effects but cannot erase committed ones. Approval denial persists across the applicable run tree.

## Sandbox and data security

The API and worker have no Docker socket or host mounts and drop Linux capabilities. The broker alone owns the Docker socket and private state volume: it is a trusted privileged boundary. Generated Python has no host fallback; containers share the host kernel and are not VMs.

Toolkit jobs use a fixed image, no network/host mounts, read-only root, resource limits and bounded output collection. V3 projects use a separate fixed image:

```bash
sudo -n docker build -f sandbox/Dockerfile.project -t agent-runtime-dev:python-v3 .
```

Before project work, verify that the operator digest in `config/general.json` exists in Docker, then use the startup wrapper. Workers and broker enforce that digest. Image policy changes need a deliberate versioned rollout; keep images referenced by snapshots. Project commands accept bounded Python/pytest argv, not a shell API; network access and package installation are unsupported.

Keep broker/application state across restarts so interrupted jobs can reconcile. Downloads validate immutable bytes. This is one authenticated workspace, not multi-tenant ownership.

Telemetry is off by default. `OTEL_EXPORTER_OTLP_ENDPOINT` enables allowlisted metadata spans excluding prompts, responses, arguments, exception bodies and credentials. PostgreSQL and Temporal histories still contain task content and require private storage. See [validation restrictions](validation-index.md#campaign-restrictions) before any acceptance work.

## Installed extensions and ambiguous effects

The [harness operator guide](harness-foundations.md#tools-and-mcp) covers `EXTENSION_REGISTRY_FILE`, schema/version pinning, environment-based credentials, and deployment compatibility. Ambiguous external writes remain visible through `/v1/runs/{id}/effects` as `outcome_unknown`; they block completion and further external mutations. Inspect the durable operation and upstream idempotency record before any manual resolution. Do not repeat the write with a new operation identity. Missing handler/context/backend versions fail closed.
