# Resource policy without additional model instructions

RunWeave can enforce compute limits, share capacity across children, and pause for an authorized increase without asking a model to interpret permission rules. New v3 policies use shared allocation and durable resource waits by default. Existing configurations with `general.resources: null` retain their legacy allocation and stop behavior. Persisted runs retain their effective limits and policy snapshots.

## Configure budgets and shared allocation

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

Compute task budgets are optional: omitted or null fields impose no task-specific cap. Define a positive `general.limits` value only when that task budget is intended. The runtime still has finite, inspectable infrastructure ceilings: the operator's `config/general.json.resource_ceilings` and the selected model registration's total-token ceiling. The checked-in live model registrations allow 128,000 total tokens and 65,536 serialized context bytes; the deterministic test registration retains 16,000 tokens and 24,576 context bytes. These are operator configurations, not provider-native model limits. `/resources.task_limits` preserves null versus explicitly declared values, `/resources.limits` shows the effective capacity, and `sources` identifies the budget or ceiling responsible. A pause includes `limit_type: "budget"` or `"ceiling"`. These ceilings are pinned at submission; increasing a run limit cannot bypass them. File/storage limits, child count/depth, context bounds, sandbox limits, and per-request timeouts remain separate controls.

## Automatic task sizing

New agents default to `adaptive: true`. A caller can submit an ordinary task through the same API without choosing a size profile. The runtime estimates initial demand from input bytes, attachment metadata, workspace presence, and declared criteria/checks. This is a deterministic sizing heuristic, not another model call or a semantic complexity classifier.

A small task starts with up to 1,024 output tokens; larger inputs start with up to 2,048. Observed output use near the current cap increases the allowance for the next call. A provider response marked truncated is charged and retried with a larger output allowance before any partial proposal can execute. Growth stops at explicit `max_tokens` or the pinned registration ceiling (currently 4,096 for the checked-in live models). An unfinished response at that ceiling stops execution. Each physical request is accounted for, and ambiguous usage remains reserved. Context capacity grows from 24,576 bytes up to the registered ceiling as serialized demand increases; larger source files must still be read in bounded portions.

`/budget.adaptive` (and v3 `/resources.adaptive`) exposes the initial input measurements, current output/context settings, revision, and working resource estimates. `resources.adapted` events record changes without copying prompt or file contents. Working estimates expand within hard grants; they are not additional spending authority. Explicit `general.limits`, toolkit budgets, file grants, approvals, selected model, and reasoning settings remain enforced independently.

For omitted v3 compute budgets, `config/general.json.adaptive_ceilings` currently pins at most 64 model attempts, 256 tool attempts, 64 commands, 128,000 total tokens, and 1,800 active seconds, further bounded by operator and model ceilings. Toolkit defaults resolve to 24 model attempts, 96 tool calls, and 600 seconds, with the registration's total-token ceiling. Explicit values remain hard caps. At a hard resource limit, existing pause/fail behavior applies; adaptation cannot approve writes or authorize a budget increase.

`adaptive: false` disables sizing and output retries, using the previous omitted toolkit defaults (6 requests, 6 tool calls, 1,024 output tokens, 120 seconds). Existing submitted runs and registrations stay pinned. For automatic output sizing, create the agent with `max_tokens` omitted or null. Existing agents with an explicit value keep that cap for future submissions.

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

The runtime persists `resources.paused`, `resources.updated`, and `resources.resumed` events. A pause identifies the resource, root/child scope, source, limit, usage, held reservation, and required capacity. Temporal waits and retries the same durable operation. `client.wait()` waits through temporary in-flight reservation shortages; it returns a budget pause when an actual effective limit requires attention. It does not ask the model to request permission, summarize policy, or repeat completed tool effects. In-flight model results and completion proposals remain reusable.

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

Token admission uses the pinned context policy's reservation estimate and reported usage, not a guarantee of exact provider billing. An unknown reservation is not refunded automatically. The [operator recovery interface](operator-recovery.md) can replace its token reservation with evidenced usage on a stopped tree, while retaining the model-attempt charge.

## Permissions and token cost

Tool grants, file scopes, effect approvals, and sandbox checks still execute in code. Models receive their authorized capabilities, relevant schemas, current task state, and compact remaining counters. Resource update endpoints, ceiling configuration, pause settings, and policy explanations are not added to model instructions. Shared mode omits redundant child-allocation fields.

The unit tests compare model-visible payloads for matching explicit limits to detect policy overhead. Token estimates exclude provider protocol framing and are not provider bills.

The policy mechanism itself adds no model calls. Allowing a previously capped task to continue can naturally increase that task's total execution spend.

## Validation

Coverage is in `tests/test_resources.py` and `tests/test_resources_integration.py`: shared/fixed allocation, concurrent spending, root/child limits, idempotent authenticated updates, ceilings, token payloads, typed non-dispatch versus unknown outcomes, active-time accounting, in-flight results, completion and effect approval recovery, pause/resume at model/tool/review boundaries, cancellation, timeout, fail mode, and Temporal replay. Tests use fake models and isolated PostgreSQL/Temporal workers.

Follow [testing and validation](validation-index.md) to run these checks. Model accuracy and runtime policy enforcement are separate evaluation targets.
