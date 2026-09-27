# Resource policy without additional model instructions

RunWeave can enforce compute limits, share capacity across children, and pause for an authorized increase without asking a model to interpret permission rules. This is an opt-in v3 policy. Existing configurations with `general.resources: null` retain their legacy allocation and stop behavior.

## Enable shared allocation

```python
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig

config = AgentConfig(
    name="shared-worker",
    provider="fake",
    model="deterministic",
    tools=["add"],
    general=GeneralPolicy(
        resources={"allocation": "shared", "on_limit": "pause"},
        limits={"model_attempts": 30, "tool_attempts": 100},
        delegation={"tools": ["add"]},
    ),
)
```

Choose a registered live provider/model for language tasks. Enabling this policy does not select a model, make a model call, or expand a tool/file grant.

`shared` means actual spending across the entire tree is admitted atomically against the root ledger. A child's `limits` are estimates: it can exceed an estimate while shared capacity remains. Creating children does not pre-spend their estimates. `Run.assignment.limits` and the legacy `effective_grants.limits` field retain assignment values; use `/resources` or `/budget` for authoritative shared capacity. With `allocation: "fixed"`, assignment limits are also enforced as local caps. Limits in the delegation policy bound authorized fixed child increases.

Limits remain finite and inspectable. Omitting a limit uses the existing declared `GeneralLimits` default; it does not mean unlimited. The operator's `config/general.json.resource_ceilings` determines allowable compute limits for new opted-in runs. The selected model registration also bounds total tokens. These ceilings are pinned at submission; increasing a run limit cannot bypass them. File/storage limits, child count/depth, context bounds, sandbox limits, and per-request timeouts remain separate controls.

## Inspect and resume

```python
state = await client.resources(run.id)
# state includes version, limits, ceilings, sources, usage,
# finalization_reserve, max_pause_seconds, and the current pause.

paused_or_finished = await client.wait(run.id)
if paused_or_finished.status == "paused_budget":
    state = await client.resources(run.id)
    await client.update_resources(
        run.id,
        expected_version=state["version"],
        limits={"model_attempts": 40},
        idempotency_key="approved-increase-1",
    )
    result = await client.wait(run.id, stop_at_budget=False)
```

Only increase the resource identified in `pause.block` when that increase is intended and within `ceilings`. Increasing a different resource does not resolve the original shortage. At an operator or registration ceiling, cancel or finish the run under the current policy; changing operator configuration does not retroactively loosen a pinned run.

The endpoints are `GET /v1/runs/{id}/resources` and `PUT /v1/runs/{id}/resources`. They use the existing workspace authentication. PUT requires `Idempotency-Key` and `{"expected_version": 1, "limits": {"model_attempts": 40}}`. Updates only increase positive integer compute limits and use compare-and-swap on the tree's version. An identical retry returns its original receipt; conflicting reuse, stale versions, decreases, terminal runs, and ceiling violations are rejected. For a fixed child, PUT to the child ID changes its local limits within the pinned delegation ceilings. Shared children use the root endpoint.

The runtime persists `resources.paused`, `resources.updated`, and `resources.resumed` events. A pause identifies the resource, root/child scope, source, limit, usage, held reservation, and required capacity. Temporal waits and retries the same durable operation. It does not ask the model to request permission, summarize policy, or repeat completed tool effects. In-flight model results and completion proposals remain reusable.

`on_limit: "fail"` is available when a terminal stop is preferred. The default pause allowance is 86,400 seconds across the tree, configurable from 1 to 604,800. Exhausting it stops with `resource_pause_timeout`. Budget waits do not consume active time. Approval and budget waits may overlap without double-counting elapsed time, and cancellation still terminates the tree.

## Finalization and accounting

The declared default finalization reserve is 1 model attempt, 2 tool attempts, 2 command attempts, and 3,584 tokens. An enabled completion reviewer adds one protected model attempt. Model/token reserves protect root finalization from child spending; tool/command reserves protect project integration and verification. The tool/command reserve is inactive for tasks without project capabilities or delegation. Root finalization may consume its protected capacity. Inspect `finalization_reserve` for the effective values; configure `general.resources.finalization` to change them, including zero. The legacy implicit reporting threshold does not apply to opted-in runs.

Model accounting distinguishes:

| Outcome | Accounting |
| --- | --- |
| Successful response | Replace the token reservation with reported usage. |
| Confirmed refusal before dispatch | Release the reservation and model-attempt charge once. |
| Timeout, cancellation, or uncertain dispatch | Retain the reservation and attempt charge. |
| Another in-flight request holds required capacity | Persist a capacity wait and recheck after settlement. |

A trusted adapter can raise `RequestNotDispatched`; the runtime also recognizes it through an SDK's explicit exception cause chain. Text, HTTP status, and generic network failures cannot claim this refund. The evaluation transport uses this type when its immutable guard refuses a request before sending it. Historical campaign manifests and ledgers remain unchanged.

Token admission uses the pinned context policy's reservation estimate and reported usage, not a guarantee of exact provider billing. An unknown reservation is intentionally not refunded automatically; there is no new manual reconciliation endpoint in this change.

## Permissions and token cost

Tool grants, file scopes, effect approvals, and sandbox checks still execute in code. Models receive their authorized capabilities, relevant schemas, current task state, and compact remaining counters. Resource update endpoints, ceiling configuration, pause settings, and policy explanations are not added to model instructions. Shared mode omits redundant child-allocation fields.

The deterministic comparison uses the same model-visible instructions, message content, and compact output schema, counted with `o200k_base`. Internal message timestamps and provider protocol framing are excluded. These are fixture input measurements, not live provider bills:

| Scenario | Legacy input tokens | Shared input tokens | Difference |
| --- | ---: | ---: | ---: |
| Direct task | 647 | 647 | 0 |
| Workspace read | 758 | 758 | 0 |
| Delegating parent | 1124 | 1059 | -65 |
| Child | 792 | 745 | -47 |

The policy mechanism itself adds no model calls. Allowing a previously capped task to continue can naturally increase that task's total execution spend.

## Validation

Coverage is in `tests/test_resources.py` and `tests/test_resources_integration.py`: shared/fixed allocation, concurrent spending, root/child limits, idempotent authenticated updates, ceilings, token payloads, typed non-dispatch versus unknown outcomes, active-time accounting, in-flight results, completion and effect approval recovery, pause/resume at model/tool/review boundaries, cancellation, timeout, fail mode, and Temporal replay. Tests use fake models and isolated PostgreSQL/Temporal workers.

See the [initial unpaid validation](resource-policy-validation-2026-09-26.md) and subsequent [paid validation and completion correction](resource-live-validation-2026-09-26.md). The paid follow-up passed pause/resume and parallel completion under their existing scenario caps; this small sample does not establish general reliability. Updated services have not been deployed.
