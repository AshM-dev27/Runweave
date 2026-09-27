# Independent Agents API — current plan

## Objective

Build a self-hosted API that turns a user request, configuration and context into authorized tool use, optional delegation, verification and a result.

## Current scope

- One authenticated workspace with explicit provider/model selection, owned public contracts, and Python client/CLI access.
- Durable run submission, session continuation, ordered replayable SSE, approvals, cancellation and bounded execution.
- Tool discovery, artifacts, isolated project workspaces, iterative task state and evidence-based completion.
- Optional scoped specialists with separate context and branches: depth one, at most two lifetime children, shared root limits and integrated verification.
- Generated code runs in an isolated offline sandbox. External mutations require idempotency or reconciliation; repository results are downloadable, not external repository writes.
- Automatic model routing, multi-tenancy, dashboards, recursive delegation and arbitrary network/package access from generated code remain outside this scope.

## Architecture

FastAPI exposes framework-independent Agent, Session, Run and Event contracts. PostgreSQL stores application state, immutable configuration snapshots, projects and ordered events; a transactional outbox bridges durable submission and workflow dispatch.

Temporal coordinates deterministic workflows and recovery. PydanticAI sits behind private adapters; model and tool I/O executes through activities. Root-owned budgets and policy apply across retries, approvals and children. Completion checks server-stored evidence against the current project revision.

The sandbox broker executes bounded commands in disposable containers reconstructed from committed revisions. Credentials stay out of public events, traces and generated-code environments.

## Resource controls — 2026-09-26

Implemented opt-in shared allocation, operator-configurable compute ceilings, typed model-reservation settlement, and durable resource pause/resume through authenticated versioned updates. Policies stay outside model instructions; shared child estimates no longer impose hard caps. Tool grants, effect approvals, and sandbox enforcement remain authoritative. The initial source-frozen full suite passed **323 unpaid tests with 4 paid skips**. Subsequent paid validation passed **2/4 scenarios using 17/17 requests** and exposed repeated work. A compact-context correction then passed both targeted scenarios using **14/15 requests**, including parallel final acceptance with children exceeding their allocation estimates. The corrected source passed **246 default tests** and **57 focused tests with services**; these overlapping suites are separate from the initial full integration baseline. Isolated test resources were cleaned up; no production rollout occurred. See [resource policy](docs/resource-policy.md), [initial validation](docs/resource-policy-validation-2026-09-26.md), and [paid validation](docs/resource-live-validation-2026-09-26.md).

## Foundation state — 2026-09-24

**Development build; not production-ready.** Priorities 1–3 are implemented and have received a second review covering review binding, source selection, Unicode/history limits, retrieval relevance, tool timeouts, cancellation, concurrent retries and cleanup races. The complete unpaid service suite passed **297 tests with 4 paid skips**. See the [follow-up review](docs/harness-review-2026-09-24.md).

A fresh live smoke test passed **3/4 scenarios using 13/16 physical requests** under the same limits as the original foundation evaluation. Review acceptance, contradiction rejection and context recall passed. The parallel root joined and merged both completed children, then passed both file checks without duplicate writes, but exhausted its evaluation allocation before final acceptance. This remains a completion-efficiency limitation. All four root histories replayed; the source stayed unchanged. Test resources were cleaned up and no production rollout was performed.

The original foundation campaign remains **3/4** and the earlier cross-model matrix remains **11/15**. They use distinct source snapshots and fixtures and are not an aggregate success rate. All three campaigns are retained and terminal; see the [validation index](docs/validation-index.md).

## Next priorities

- Broaden parallel completion reliability testing within existing budgets; the September 26 targeted follow-up now reaches final acceptance, but one passing sample does not establish reliability.
- Broaden semantic review accuracy and long-context retrieval evaluation before enabling review by default.
- Add concrete execution backends and a supported operator reconciliation interface as later scope requires.
- Establish deployment-readiness evidence. Use fresh bounded paid smoke campaigns for model-related changes under the user's [standing preference](docs/validation-index.md#paid-smoke-test-preference); never reopen terminal campaigns or transfer their allowances.

## Implemented foundations — 2026-09-23

Implemented priorities 1–3 from the capability review:

1. Completion reliability: explicit pending-check guidance, reuse of successful checks, and optional durable semantic review. Deterministic evidence remains authoritative; uncertain or unavailable review cannot approve a result.
2. Context management: bounded extractive summaries, relevance-selected history with source references and retrieval, compact observations, and versioned token reservation policies.
3. Extension interfaces: operator-installed versioned handlers and HTTP MCP tools, pinned skills, context policies and execution backends. Discovery never increases grants; external effects require approval and explicit retry/reconciliation contracts.

Validated with fake models, API contracts, isolated PostgreSQL/Temporal recovery and replay, and a separately recorded bounded live evaluation using the existing provider configuration (authorized 2026-09-23). Retained terminal campaigns remain untouched. Configuration and limitations are in [harness foundations](docs/harness-foundations.md).

## Documentation

- [AGENTS.md](AGENTS.md): concise development, model and security rules.
- [README.md](README.md): setup and entry points to usage and operations.
- [Historical plan through 2026-09-20](docs/history/plan-through-2026-09-20.md): complete prior scope, decisions and outcomes; historical approvals are not current authorization.
