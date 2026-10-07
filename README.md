# Runweave

**One task API for agents that use tools, work with files, and reuse isolated computers.**

Give an agent a task from your application. Runweave coordinates multiple model and tool actions, persists progress, enforces permissions and resource limits, and returns an answer, downloadable files, or an explicit next step. You choose the model and connect the capabilities your application needs. Work survives worker interruptions, and named computers can retain working files for later tasks in the same conversation.

Use it for workflows such as invoice reconciliation, support actions that need approval, or research that produces structured outputs. Your application supplies the business rules and authorized integrations; Runweave supplies the execution and control layer.

## What you get

- **One task call:** submit inputs and files with `client.run()`, then receive an answer, output files, or an explicit approval/resource decision. Python, HTTP, and CLI interfaces use the same run lifecycle.
- **Durable execution:** persistent sessions, replayable SSE progress events, retry-safe submission, cancellation, and recovery after worker restarts. Runs retain their configuration and policy snapshots.
- **Adaptive budgets:** automatically size work and output allowances from the request and observed demand, within caller, model, and operator ceilings. Hard limits return an explicit pause or failure. See [resource controls](docs/resource-policy.md).
- **Action controls:** authorized tools, file scopes, and human approvals with exact arguments and receipt-backed previews. Decisions remain explicit; see [outcome handling](docs/usage.md#handle-the-outcome).
- **Scoped delegation and sandboxing:** child agents share resource limits and receive bounded file/tool grants. Project commands run in isolated containers; results are integrated and verified. See [workspaces and delegation](docs/usage.md#workspaces-artifacts-and-inspection).
- **Checkable results:** exact-text or JSON Schema contracts, revision-bound verification receipts, batched checks, optional semantic completion review, and exportable evidence with offline verification. See [result contracts](docs/acceptance-contracts.md) and [completion checks](docs/harness-foundations.md#completion).
- **Context and continuation:** bounded conversation memory, retrieval of earlier session messages, and receipt-derived progress guidance to help agents use existing observations and avoid repeated reads. See [context and sessions](docs/harness-foundations.md#context-and-sessions).
- **External-effect recovery:** durable operation identities and receipts support safe retries or reconciliation. Unresolved writes block completion; operators can reconcile eligible effects and uncertain model usage. See [operator recovery](docs/operator-recovery.md).
- **Extensible capabilities:** built-in file/project tools, operator-installed HTTP MCP tools and skills, plus versioned context and execution interfaces. See [tools and extensions](docs/harness-foundations.md).
- **Hosted browsing:** optional Browser Use Cloud tasks with explicit approval, a $1 cap per hosted task, validated structured results, and cancellation/cleanup. See [browser integration](docs/browser-use.md).
- **Durable files:** stream text, PDF, XLSX, images and binary artifacts with immutable hashes and bounded quotas. Published files survive computer closure and worker replacement; the API and workers share persistent file storage. Format support does not install parsing libraries. See [file storage](docs/artifacts.md).
- **Remote Python:** optional disposable E2B jobs process authorized artifacts, publish files and terminate. Internet is disabled; interrupted dispatches and publication reconcile against saved identities. See [E2B integration](docs/e2b.md).
- **Reusable computers:** named E2B computers retain guest files across actions and later tasks in the same conversation. Commands run exclusively, each in a fresh Python interpreter, with bounded warm lifetime, idle expiry and explicit close. Only E2B is implemented; pause/resume and desktop automation are future work. See [computer sessions](docs/computer-sessions.md).
- **Model choice:** registered OpenAI and Anthropic adapters, OpenAI-compatible Chat Completions, registry-configured OpenAI reasoning effort, and a scripted fake provider for unpaid tests. Provider credentials belong in the worker environment; see [provider setup](docs/operations.md#providers-and-configuration).

Development preview: the current scope is one authenticated workspace. Multi-tenancy and automatic model routing are not implemented. Review [validation and limitations](docs/validation-index.md) before deploying; acceptance checks do not guarantee factual correctness.

## Quickstart

Requires Python **3.12–3.13**, **uv 0.11.12**, and Docker Compose **2.24+** with daemon access (or non-interactive `sudo -n docker`). From the repository root:

```bash
uv sync --frozen
test -e .env.local || cp .env.example .env.local
uv run python -m scripts.start_local
uv run --env-file .env.local python -m examples.quickstart
```

No model-provider key is required. Startup generates missing local authentication, preserves existing volumes, and starts the API and workers. The [complete example](examples/quickstart.py) creates a fake agent and submits a task through the public API. Its scripted output is:

```text
Completed the requested scoped task.
Run: <run-id>
Outcome: succeeded
```

The fake provider demonstrates execution; it does not interpret natural-language tasks. Keep the generated `.env*.local` files private.

API: **http://localhost:18000** · Interactive docs: **http://localhost:18000/docs**. Use **Authorize** in the docs with your local `API_KEY`.

## Use it in your application

Configure an agent once with instructions, a registered model, and authorized tools. For the example below, select a live model from `client.models()` and configure `general={}` with `tools=["computer_python", "artifact_read"]`. Install worker-side E2B credentials using the [E2B setup guide](docs/e2b.md#setup). Then save the agent ID and submit work:

```python
from pathlib import Path

result = await client.run(
    agent_id=agent_id,
    input=(
        "Use the computer named analysis to reconcile invoices against payments. "
        "Keep the working database for follow-up tasks and return a summary and exceptions CSV."
    ),
    files=["invoices.csv", "payments.csv"],
)

if result.outcome == "succeeded":
    print(result.answer)
    output = Path("output")
    output.mkdir(parents=True, exist_ok=True)
    for file in result.files:
        await client.download_file(file.id, output / file.filename)
else:
    print(result.message, result.next_action or "")
```

Within the computer's lifetime, continue on the same conversation without uploading the working database again:

```python
follow_up = await client.run(
    agent_id=agent_id,
    session_id=result.session_id,
    input="Continue on analysis. Apply these new payments and return an updated summary.",
    files=["new-payments.csv"],
)
```

Guest files are temporary. Publish anything that must survive expiry, and close unused computers with `client.close_computer(id)`; closure is asynchronous. A later task must still grant `computer_python`, and input artifact authorization applies to each run. See [computer ownership and cleanup](docs/computer-sessions.md).

For a disposable file task, grant `e2b_files`. For small CSVs with `invoice_id,amount` columns, the toolkit tool `csv_analyze` remains available with `general=None`. `client.tools()` lists toolkit tools; `client.installed_capabilities()` lists installed general capabilities. Both use the same task API. See [agent configuration](docs/usage.md#general-runtime-configuration) and [provider setup](docs/operations.md#providers-and-configuration).

Approval and budget pauses return to your application for a decision. They are never approved or increased automatically. Use `client.result(run_id)` to resume waiting; a client timeout does not cancel server work. See [outcome handling](docs/usage.md#handle-the-outcome) and [retry recovery](docs/usage.md#interrupted-requests-and-recovery).

The same contracts are available over HTTP: create an agent with `POST /v1/agents`, submit tasks with `POST /v1/runs`, and retrieve results or replay events by run ID. The Python client handles these steps for you. A CLI is also included:

```bash
uv run --env-file .env.local python -m agent_runtime.cli run AGENT_ID "Your task"
uv run --env-file .env.local python -m agent_runtime.cli result RUN_ID
```

### Task lifecycle

![Runweave task lifecycle: submit and persist inputs, decide from current context, enforce grants and budgets, execute activities, check results and return an explicit outcome. Computer actions publish durable files; reusable machines retain temporary working files. Pauses, interruptions, failures and cleanup have explicit controls.](docs/assets/runweave-task-lifecycle.svg)

General tasks repeat the decision and execution loop until they pass configured acceptance requirements or reach a stop condition. Execution status and task outcome are separate: a `completed` task with an unresolved tool failure requires attention. A model's completion assessment cannot override recorded tool errors or approval denial.

## How it runs

![Runweave architecture: clients call FastAPI; PostgreSQL holds application state, artifact references, computer ownership and admission. The worker dispatcher delivers committed outbox records to Temporal. Activities perform model and tool I/O through private adapters to model providers, MCP and hosted browser services, local isolated project jobs and E2B computers. The API and activities share persistent artifact byte storage.](docs/assets/runweave-architecture.svg)

FastAPI owns the public contracts. PostgreSQL stores runs, ordered events, receipts, artifact metadata, computer ownership and held admission slots. The worker dispatcher delivers the transactional outbox to Temporal; deterministic workflows schedule activities for all model and tool I/O. Private adapters connect PydanticAI models, installed tools and isolated execution providers. Project commands use the trusted Docker broker; remote Python uses E2B.

Published file bytes live in shared persistent storage; PostgreSQL keeps their identities and hashes. The included backend is a shared filesystem, configured as a named volume in local Compose. Deploying workers on separate hosts requires a shared backend. Back up file bytes with PostgreSQL, Temporal and broker state. Guest computer files are a separate, temporary workspace.

Runweave is the coordinator. External services and MCP servers provide application-specific capabilities; tools must be installed by an operator and explicitly granted to an agent.

### Deployment boundaries

The [single-workspace deployment profile](docs/production.md) provides an immutable-image configuration, separate API/worker credentials, migration, monitoring and recovery procedures. The local quickstart is for development. The current build is a development preview; this profile does not establish SaaS readiness.

| Default policy | Scope |
| --- | --- |
| 20 active runs | One authenticated workspace; one active run per conversation |
| Four E2B computers | Shared by disposable jobs, warm sessions and unresolved cleanup |
| 15-minute computer lifetime; 2-minute idle expiry | Fixed hard expiry with successful-action idle refresh |
| Two computers and 32 actions per computer | Per conversation and per computer, respectively |
| 20 seconds per Python command; 512 MiB guest Python memory | Installed file/computer registration |
| 16 MiB per artifact; eight produced files and 64 MiB per root task tree | Workspace published-artifact quota defaults to 512 MiB |

These are policy defaults, not measured throughput or provider account quotas. Public schemas remain independent of PydanticAI, Temporal and provider SDKs. Modal and Fly.io require additional reviewed adapters; they are not available backends. Tenant isolation, fair scheduling and usage metering are deferred to the [SaaS milestone](PLAN.md#deferred-saas-milestone).

## Documentation

| Guide | Covers |
| --- | --- |
| [Usage](docs/usage.md) | Python, HTTP, CLI, sessions, files, approvals, and recovery |
| [Resource policy](docs/resource-policy.md) | Automatic task sizing, hard limits, and pause/resume |
| [Result contracts](docs/acceptance-contracts.md) | Structured outputs and offline evidence verification |
| [Tools and extensions](docs/harness-foundations.md) | MCP, skills, context policies, and execution backends |
| [Browser Use Cloud](docs/browser-use.md) | Optional hosted browsing with approval and cost caps |
| [E2B](docs/e2b.md) | Bounded remote Python, a single-task invoice demo, recovery and cleanup |
| [Reusable computers](docs/computer-sessions.md) | Conversation ownership, saved guest files, lifetime, explicit close and recovery |
| [Artifacts](docs/artifacts.md) | Streaming transfer, supported formats, hashes, quotas, storage and backups |
| [Operations](docs/operations.md) | Deployment, credentials, sandboxing, and service recovery |
| [Single-workspace deployment](docs/production.md) | Bounded E2B deployment, monitoring, release and restore procedures |
| [Operator recovery](docs/operator-recovery.md) | Reconcile uncertain effects and model usage |
| [Validation](docs/validation-index.md) | Reproducible checks and known limitations |

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and tests, [AGENTS.md](AGENTS.md) for code boundaries, and [PLAN.md](PLAN.md) for current scope. Default tests use fake models and require no provider credentials. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

[Apache License 2.0](LICENSE).
