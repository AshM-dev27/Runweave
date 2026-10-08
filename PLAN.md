# Runweave scope and roadmap

Runweave is a self-hosted API for turning a task into authorized tool use, durable execution and a result. The public interface belongs to Runweave; model and workflow frameworks remain implementation details.

## Current scope

- One authenticated workspace, explicit model selection, a Python client and a CLI.
- Durable task submission, session continuation, ordered events, approval waits, cancellation and recovery.
- Adaptive task budgets within operator ceilings, shared across retries and scoped child agents.
- Operator-installed tools and MCP integrations, file artifacts and isolated Python project workspaces.
- Optional completion review, caller-defined result contracts and portable evidence with an offline verifier.
- Optional Browser Use hosted tasks with approval, a provider-run cost cap, polling and cleanup.
- Optional bounded E2B Python jobs with internet disabled, durable phase/handle recovery and sandbox cleanup; file-based jobs use attached artifact references and publish downloadable outputs.
- Binary and text artifacts with streaming transfer, immutable hashes, configurable byte quotas and optional shared filesystem blob storage. Existing inline artifacts remain readable.
- Single-workspace E2B deployment profile with durable provider admission, operational status and cleanup-only operator recovery.
- Named conversation-owned E2B computers reusable across actions and later tasks, with exclusive use, bounded warm lifetime, idle expiry and durable cleanup.

PostgreSQL stores application state, artifact references, computer ownership and a transactional submission outbox. Published bytes use optional shared filesystem storage, with existing inline files retained. Temporal coordinates deterministic workflows; model and tool I/O runs in activities. External writes require idempotency or receipt reconciliation. Generated project code runs through the isolated sandbox broker; remote Python runs through private E2B adapters.

Automatic model routing, multi-tenancy, a dashboard, recursive delegation, and arbitrary network or package access from generated code are outside the current scope. Child delegation is limited to depth one and two lifetime children per root.

## Maturity

This is a development build. The test suite covers API contracts, execution limits and recovery paths with fake models and local services. Live-model correctness, security under adversarial workloads, load capacity and production operations need broader validation. A completed run or a passing output schema does not guarantee that its answer is correct. See [validation](docs/validation-index.md) for reproducible checks and their limits.

## Next priorities

Artifact-based execution and bounded reusable computer sessions are implemented. The next work is to make broader tasks practical: curated execution templates with discoverable packages/formats, lower phase-transition latency, quota preflight with specific errors, and checkpoint/pause policies that preserve durable ownership and cleanup accounting. Two real business connections and held-out live-model task evaluations follow this foundation; connector selection requires a concrete target workflow.

1. Expand held-out task evaluations across models with fixed budgets, independent expected results and false-acceptance measurements.
2. Establish production deployment guidance backed by upgrade, restore, load and failure-recovery testing.
3. Improve evidence freshness and long-context retrieval while retaining conservative defaults.
4. Add execution backends and receipt adapters when concrete integrations need them.

## Deferred SaaS milestone

Revisit admission and concurrency controls before a multi-tenant SaaS launch. The current 20-run and four-E2B-job settings are deployment policies, not measured capacity or SaaS plan limits.

- Establish tenant-scoped authentication, authorization, data, credentials and idempotency identities.
- Separate queued submissions from execution capacity, with fair scheduling and per-tenant/provider quotas.
- Replace the shared workspace admission lock with coordination suitable for independently scaled workers while preserving atomic admission and recovery accounting.
- Add tenant usage metering and establish supported limits through workload, sustained-load and failure-recovery benchmarks, including provider quotas and cleanup backlog.

See [README](README.md) for setup, [usage](docs/usage.md) for API examples, and [operations](docs/operations.md) for deployment boundaries.
