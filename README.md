# Runweave

**A self-hosted, model-agnostic runtime API for durable AI agents.**

Build and run agents with tools, persistent sessions, isolated workspaces, human approvals, delegation, streaming events, and provider-independent models — behind one API.

## Capabilities

- Durable run submission and session continuation, with idempotent submission retries.
- Ordered, persisted events that clients can replay over server-sent events (SSE).
- Tool permissions, approval decisions, cancellation, and bounded execution.
- Uploadable artifacts, downloadable results, and isolated project workspaces.
- Scoped delegation with shared resource limits and verification of integrated results.
- Completion checks against stored evidence for the current project revision, with optional semantic review.
- Bounded, retrievable conversation memory and operator-installed tools, HTTP MCP, and skills.

The current scope supports one authenticated workspace with explicit provider selection. Multi-tenancy, automatic model routing, and arbitrary network or package access from generated code are outside that scope.

## Architecture

![Three-layer architecture: clients submit through FastAPI to PostgreSQL; the outbox dispatches Temporal workflows with optional scoped subagents. Workflows schedule model and tool activities; tools access isolated containers through a sandbox broker. The task loop progresses from request to plan, action, verification, and result, returning to planning when needed.](docs/assets/architecture-overview-v2.png)

*Clients submit tasks; durable workflows coordinate tools and return results.*

FastAPI exposes public Agent, Session, Run, and Event contracts. PostgreSQL stores application state and ordered events; a transactional outbox connects persisted submissions to workflow dispatch.

Temporal coordinates deterministic workflows and recovery. Model and tool I/O runs through activities, with PydanticAI behind private adapters. Public API schemas stay independent of both execution frameworks. Generated code executes in isolated containers under bounded permissions and resources.

## Start locally

From the root of a Git checkout, use a POSIX environment with Python **3.12–3.13**, **uv 0.11.12**, and Docker Compose with daemon access (or non-interactive `sudo -n docker`).

```bash
uv sync --frozen
test -e .env.local || cp .env.example .env.local
uv run python -m scripts.start_local
```

The startup wrapper preserves existing credentials and volumes, generates a missing app `API_KEY` and sandbox authentication, and waits for local services. Startup makes no model calls. Keep `.env.local` private; see [operations](docs/operations.md#startup-and-recovery) for credential handling and recovery.

API: **http://localhost:18000** · Interactive API docs: **http://localhost:18000/docs**.

## Try a scripted fake task

With local services running:

```bash
uv run --env-file .env.local python -m agent_runtime.cli readiness
uv run --env-file .env.local python -m agent_runtime.cli agent --tools add
# Replace AGENT_ID with the returned ID:
uv run --env-file .env.local python -m agent_runtime.cli submit AGENT_ID 'add 2 3' --key demo-1 --wait
```

The default `fake/deterministic` provider uses scripted actions to add two numbers without a paid model call. This demonstrates the submission and tool lifecycle, not general language autonomy.

Reuse the idempotency key only when retrying the same submission; use a new key for a new task. Readiness checks local dependencies but does not verify provider or MCP access.

## Usage guidelines

- Configure task instructions, an explicit provider/model, and authorized tools in an [agent configuration](docs/usage.md#general-runtime-configuration). Use the fake provider for scripted examples; select a registered live provider for language tasks.
- [Attach inputs and workspaces](docs/usage.md#workspaces-artifacts-and-inspection) when needed: reattach artifacts and specify the workspace/revision on each submission or continuation.
- When a run pauses for approval, [review the exact tool arguments](docs/usage.md#python-client-and-api) before approving or denying the request.
- Track progress with `watch`, retrieve results with `get` or `wait`, and inspect outputs before continuing. Use [`continue`](docs/usage.md#python-client-and-api) for a new turn in the same session, with a new idempotency key.

## Documentation

- [Usage](docs/usage.md): Python/API examples, CLI commands, artifacts, and workspaces.
- [General runtime configuration](docs/usage.md#general-runtime-configuration): instructions, authorized tools, delegation, and completion checks.
- [Resource policy](docs/resource-policy.md): shared compute budgets, durable pause/resume, and server-side enforcement without extra model instructions.
- [Harness foundations](docs/harness-foundations.md): review, context policies, tool/MCP registration, skills, and execution backends.
- [Operations](docs/operations.md): provider setup, execution limits, recovery, and sandbox security.
- [Validation index](docs/validation-index.md): recorded checks, evidence, and limitations.
- [Current project plan](PLAN.md): scope, current state, and next priorities.

## Contributing

1. Read [AGENTS.md](AGENTS.md) for project conventions and scope your work against [PLAN.md](PLAN.md).
2. Keep changes focused on the task and follow the existing public contracts and workflow boundaries.
3. Use fake models for tests by default; cover API contracts and recovery paths when your changes affect them.
4. Update relevant documentation and run the applicable [local checks](docs/validation-index.md#local-checks) and [CI checks](.github/workflows/ci.yml). Integration checks require local services.

## License

Licensed under the [Apache License 2.0](LICENSE).

## TODO

Development build; not production-ready, with live acceptance incomplete—see the [latest harness review](docs/harness-review-2026-09-24.md) and [earlier cross-model results](docs/completion-models-findings.md).

Outstanding work from [PLAN.md](PLAN.md#next-priorities), plus publication preparation:

- [x] Distinguish pending checks, reuse current verification receipts, and add optional semantic review.
- [x] Add bounded context/retrieval and versioned token reservations within existing shared limits.
- [x] Add versioned extension interfaces for tools/MCP, skills, context, and execution.
- [ ] Broaden live acceptance, including reliable parallel/subagent completion.
- [ ] Complete remaining evaluation and recovery/replay checks, and establish deployment-readiness evidence.
