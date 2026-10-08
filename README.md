# Runweave

**One task API for agents that use tools, work with files, and reuse isolated computers.**

Runweave is a self-hosted execution API for tool-using agents. Give an agent a task from your application; Runweave coordinates model and tool actions, persists progress, enforces permissions and resource limits, and returns an answer, downloadable files, or an explicit next step. You choose the model and connect the capabilities your application needs.

Use it for workflows such as invoice reconciliation, support actions that need approval, or research that produces structured outputs. Your application supplies the business rules and authorized integrations; Runweave supplies the execution and control layer.

## What you get

- **One task call:** submit inputs and files with `client.run()`, then receive an answer, output files, or an explicit approval/resource decision. Python, HTTP, and CLI interfaces share the same [run lifecycle](docs/usage.md).
- **Durable execution:** conversation history, replayable progress events, retry-safe submission, cancellation and recovery after worker restarts. Operation identities and receipts support [external-effect reconciliation](docs/operator-recovery.md).
- **Controlled actions:** explicit tool and file grants, human approvals, scoped child agents and [adaptive budgets](docs/resource-policy.md) within caller, model and operator ceilings. Project commands run in isolated containers.
- **Durable files:** stream text, PDF, XLSX, images and binary artifacts with hashes and bounded quotas. Published files survive computer closure and worker replacement. See [file storage](docs/artifacts.md).
- **Reusable computers:** named E2B computers retain working files across actions and later tasks in the same conversation. Each action uses a fresh Python interpreter, with internet disabled and a bounded lifetime. [Disposable jobs](docs/e2b.md) and [reusable sessions](docs/computer-sessions.md) share admission and cleanup controls.
- **Models and integrations:** choose registered OpenAI, Anthropic or OpenAI-compatible models; grant installed tools, HTTP MCP integrations and skills. Optional [Browser Use Cloud](docs/browser-use.md) tasks require approval and carry a $1 cap per hosted task. See [provider setup](docs/operations.md#providers-and-configuration) and [extensions](docs/harness-foundations.md).
- **Checkable outcomes:** [result contracts](docs/acceptance-contracts.md), revision-bound verification receipts, optional completion review and portable evidence. Recorded tool failures remain visible even when a model claims success.

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

The example below reconciles your local `invoices.csv` and `payments.csv`, publishes output files, and keeps the working database on a named computer for follow-up tasks. It requires a configured live model and E2B; both may incur charges. Install provider credentials on the worker using [provider setup](docs/operations.md#providers-and-configuration) and [E2B setup](docs/e2b.md#setup).

Choose a registered provider/model pair with `client.models()` or the CLI `models` command. Set `RUNWEAVE_PROVIDER` and `RUNWEAVE_MODEL` to those aliases. Save this as `reconcile.py` and run it from the checkout with `uv run --env-file .env.api.local python reconcile.py`:

```python
import asyncio
import os
from pathlib import Path

from agent_runtime.client import Client
from agent_runtime.schemas import AgentConfig


async def main():
    async with Client(
        base_url=os.environ.get("RUNWEAVE_URL", "http://localhost:18000"),
        api_key=os.environ["API_KEY"],
    ) as client:
        # Create once; save the agent ID for subsequent tasks.
        agent = await client.create_agent(
            AgentConfig(
                name="invoice-assistant",
                provider=os.environ["RUNWEAVE_PROVIDER"],
                model=os.environ["RUNWEAVE_MODEL"],
                tools=["computer_python", "artifact_read"],
                general={},
            )
        )
        result = await client.run(
            agent_id=agent.id,
            input=(
                "Use the computer named analysis to reconcile invoices against payments. "
                "Keep the working database for follow-up tasks and return a summary and exceptions CSV."
            ),
            files=["invoices.csv", "payments.csv"],
        )
        print("Agent:", agent.id, "Conversation:", result.session_id, "Run:", result.run_id)
        if result.outcome != "succeeded":
            print(result.message, result.next_action or "")
            return

        print(result.answer)
        output = Path("output")
        output.mkdir(parents=True, exist_ok=True)
        for file in result.files:
            await client.download_file(file.id, output / file.filename)


asyncio.run(main())
```

Within an authenticated client context, pass the saved agent and conversation IDs to continue while the computer is still warm. The working database stays on the computer; only the new payments need uploading:

```python
follow_up = await client.run(
    agent_id=agent_id,
    session_id=session_id,
    input="Continue on analysis. Apply these new payments and return an updated summary.",
    files=["new-payments.csv"],
)
```

Guest files are temporary. Publish anything that must survive expiry. List machines with `await client.computers(session_id)` and close unused ones with `await client.close_computer(computer.id)`; closure is asynchronous. A later task must still grant `computer_python`, and input artifact authorization applies to each run. See [computer ownership and cleanup](docs/computer-sessions.md).

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

The included Compose deployment stores published file bytes in a shared persistent filesystem volume; PostgreSQL keeps their identities and hashes. Standalone deployments without `ARTIFACT_STORAGE_PATH` retain bytes in PostgreSQL. Deploying workers on separate hosts requires a shared backend. Back up external file bytes with PostgreSQL, Temporal and broker state. Guest computer files are a separate, temporary workspace.

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
| 16 MiB per artifact; eight produced files and 64 MiB per general root task tree | Workspace published-artifact quota defaults to 512 MiB |

These are policy defaults, not measured throughput or provider account quotas. Public schemas remain independent of PydanticAI, Temporal and provider SDKs. Modal and Fly.io require additional reviewed adapters; they are not available backends. Tenant isolation, fair scheduling and usage metering are deferred to the [SaaS milestone](PLAN.md#deferred-saas-milestone).

File-format support does not install parsing packages; provision those in an approved E2B template. Computer pause/resume and desktop automation are not implemented. See [validation](docs/validation-index.md) for reproducible checks and the boundaries between fake-model tests, real execution benchmarks and live-model evaluations.

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
