# Runweave scope and roadmap

Runweave is a self-hosted API for turning a task into authorized tool use, durable execution and a result. The public interface belongs to Runweave; model and workflow frameworks remain implementation details.

## Current scope

- One authenticated workspace, explicit model selection, a Python client and a CLI.
- Durable task submission, session continuation, ordered events, approval waits, cancellation and recovery.
- Adaptive task budgets within operator ceilings, shared across retries and scoped child agents.
- Operator-installed tools and MCP integrations, file artifacts and isolated Python project workspaces.
- Optional completion review, caller-defined result contracts and portable evidence with an offline verifier.
- Optional Browser Use hosted tasks with approval, a provider-run cost cap, polling and cleanup.

PostgreSQL stores application state and a transactional submission outbox. Temporal coordinates deterministic workflows; model and tool I/O runs in activities. External writes require idempotency or receipt reconciliation. Generated code executes through an isolated sandbox broker.

Automatic model routing, multi-tenancy, a dashboard, recursive delegation, and arbitrary network or package access from generated code are outside the current scope. Child delegation is limited to depth one and two lifetime children per root.

## Maturity

This is a development build. The test suite covers API contracts, execution limits and recovery paths with fake models and local services. Live-model correctness, security under adversarial workloads, load capacity and production operations need broader validation. A completed run or a passing output schema does not guarantee that its answer is correct. See [validation](docs/validation-index.md) for reproducible checks and their limits.

## Next priorities

1. Expand held-out task evaluations across models with fixed budgets, independent expected results and false-acceptance measurements.
2. Establish production deployment guidance backed by upgrade, restore, load and failure-recovery testing.
3. Improve evidence freshness and long-context retrieval while retaining conservative defaults.
4. Add execution backends and receipt adapters when concrete integrations need them.

See [README](README.md) for setup, [usage](docs/usage.md) for API examples, and [operations](docs/operations.md) for deployment boundaries.
