# Runweave

**A self-hosted API for agents that use tools, pause for approval, and resume after interruptions.**

Give an agent a task from your application. Runweave coordinates model calls and tools, records progress, enforces permissions and resource limits, and returns an answer or an explicit next step. You choose the model and connect the capabilities your application needs.

Use it for workflows such as invoice reconciliation, support actions that need approval, or research that produces structured outputs. Your application supplies the business rules and authorized integrations; Runweave supplies the execution and control layer.

## What you get

- **One task call:** submit inputs and files with `client.run()`, then receive an answer, output files, or an approval/resource decision.
- **Durable execution:** persisted sessions, ordered events, retry-safe submission, cancellation, and recovery through Temporal.
- **Explicit control:** tool grants, human approval for configured actions, shared resource budgets, and scoped delegation.
- **Checkable results:** exact-text or JSON Schema contracts, verification receipts, and exportable acceptance evidence.
- **Extensible tools:** built-in file/project tools, operator-installed HTTP MCP servers, skills, and optional Browser Use Cloud.
- **Model choice:** registered OpenAI and Anthropic adapters, plus a scripted fake provider for unpaid tests. Credentials stay on the worker.

Development preview: the current scope is one authenticated workspace. Multi-tenancy and automatic model routing are not implemented. Review [validation and limitations](docs/validation-index.md) before deploying; acceptance checks do not guarantee factual correctness.

## Quickstart

Requires Python **3.12–3.13**, **uv 0.11.12**, and Docker Compose with daemon access (or non-interactive `sudo -n docker`). From the repository root:

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

Configure an agent once with instructions, a registered model, and authorized tools. Then save its ID and use the same call for each task:

```python
result = await client.run(
    agent_id=agent_id,
    input="Remove duplicate invoice IDs, include refunds, and return the net total and cleaned CSV.",
    files=["invoices.csv"],
)

if result.outcome == "succeeded":
    print(result.answer)
else:
    print(result.message, result.next_action or "")
```

For this invoice example, create a toolkit agent with `tools=["csv_analyze"]` and `general=None` (the default); its CSV input must have `invoice_id,amount` columns. Add live-provider credentials to the worker environment and choose a registered model from `client.models()`. `client.tools()` lists toolkit tools; `client.installed_capabilities()` lists the project/MCP capabilities for agents configured with `general={}`. Both use the same task API. See [agent configuration](docs/usage.md#general-runtime-configuration) and [provider setup](docs/operations.md).

Approval and budget pauses return to your application for a decision. They are never approved or increased automatically. Use `client.result(run_id)` to resume waiting; a client timeout does not cancel server work. See [outcome handling](docs/usage.md#handle-the-outcome) and [retry recovery](docs/usage.md#interrupted-requests-and-recovery).

The same contracts are available over HTTP: create an agent with `POST /v1/agents`, submit tasks with `POST /v1/runs`, and retrieve results or replay events by run ID. The Python client handles these steps for you. A CLI is also included:

```bash
uv run --env-file .env.local python -m agent_runtime.cli run AGENT_ID "Your task"
uv run --env-file .env.local python -m agent_runtime.cli result RUN_ID
```

## How it runs

FastAPI exposes the public API. PostgreSQL stores runs and ordered events; a transactional outbox dispatches Temporal workflows. Workflows coordinate model and tool activities through private PydanticAI adapters. Project commands execute in isolated containers through the sandbox broker. Public schemas remain independent of these execution frameworks.

Runweave is the coordinator. External services and MCP servers provide application-specific capabilities; tools must be installed by an operator and explicitly granted to an agent.

## Documentation

| Guide | Covers |
| --- | --- |
| [Usage](docs/usage.md) | Python, HTTP, CLI, sessions, files, approvals, and recovery |
| [Resource policy](docs/resource-policy.md) | Automatic task sizing, hard limits, and pause/resume |
| [Result contracts](docs/acceptance-contracts.md) | Structured outputs and offline evidence verification |
| [Tools and extensions](docs/harness-foundations.md) | MCP, skills, context policies, and execution backends |
| [Browser Use Cloud](docs/browser-use.md) | Optional hosted browsing with approval and cost caps |
| [Operations](docs/operations.md) | Deployment, credentials, sandboxing, and service recovery |
| [Operator recovery](docs/operator-recovery.md) | Reconcile uncertain effects and model usage |
| [Validation](docs/validation-index.md) | Reproducible checks and known limitations |

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and tests, [AGENTS.md](AGENTS.md) for code boundaries, and [PLAN.md](PLAN.md) for current scope. Default tests use fake models and require no provider credentials. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

[Apache License 2.0](LICENSE).
