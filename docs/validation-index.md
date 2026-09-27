# Validation and evidence

**Development build; not production-ready.** Latest paid validation on 2026-09-26 passed **2/4 scenarios using 17/17 requests** and found repeated work. After a compact-context correction, both targeted follow-up scenarios passed using **14/15 requests**. The final source passed **246 default tests** and **57 focused tests with services**. The initial resource-policy baseline passed **323 unpaid tests with 4 paid skips**. Permission/resource setup adds no input tokens in four controlled comparisons. Earlier live results remain **3/4** for September 24 and **11/15** for the cross-model matrix. These distinct fixtures and source snapshots do not establish a statistical reliability score. Test resources were cleaned up; no production rollout occurred.

## Evidence index

- [Paid resource validation](resource-live-validation-2026-09-26.md): actual pause/resume, reviewer acceptance/rejection, shared children, repeated-work correction, targeted parallel final acceptance, provider usage and cleanup. [Detailed results](resource-live-results-2026-09-26.json).

- [Initial resource policy validation](resource-policy-validation-2026-09-26.md): opt-in shared allocation, durable budget pause/resume, typed reservation settlement, and model-visible token comparisons. This initial phase used fake models only.

- [Follow-up harness review](harness-review-2026-09-24.md): reproduced and fixed review, context, tool-lifecycle and cleanup defects; concurrent retry/cancellation tests; 297-test full suite; fresh bounded live evaluation and cleanup. [Detailed results](harness-review-results-2026-09-24.json).

- [Harness foundations](harness-foundations-validation.md): completion review, bounded memory and retrieval, tool/MCP and skill extensions, and versioned policies. Full unpaid service suite: 268 passed, 3 paid skips; two additional focused regressions passed. Separate authorized live test: **3/4 passed, 13/16 requests**. Parallel root completion failed; the subsequent guidance corrections are re-evaluated in the separate 2026-09-24 report.

- [Jev harness value decision](jev-harness-value-decision-2026-09-23.md): 174 new Jev calls against retained harness fixtures, 32 duplicate-write comparisons, and 42 passing focused/offline checks. **Do not integrate into the default harness:** no demonstrated natural-error improvement and five of eleven valid answers deferred. [Detailed results](jev-harness-value-results-2026-09-23.json).

- [Fresh Jev sample benchmark](jev-sample-benchmark-2026-09-23.md): six new request families, 36 actual scripted API/Temporal completion replays, and 120 completed Jev calls on 2026-09-23. Reports accuracy, latency, cost, review burden, and uncertainty-routing failures; the Jev gate remains an offline comparison. [Results JSON](jev-sample-results-2026-09-23.json).

- [Jev fit and feasibility benchmark](jev-evaluation.md): 80 authored cases, three passes, 240 completed Jev requests on 2026-09-22. Evaluates semantic judgments only; does not change the runtime acceptance result or establish production readiness. [Machine-readable results](jev-benchmark-results.json).

- [Historical plan through 2026-09-20](history/plan-through-2026-09-20.md): preserved scope, decisions and outcomes; past approvals do not authorize new actions.

- [Completion model findings](completion-models-findings.md): latest matrices, receipts, child outcomes, limitations and raw evidence links. Baseline used 106/216 calls; verification used 96/216.
- [Completion-loop validation](completion-loop-validation.md): earlier correction/replay checks; live campaign stopped at 28/72 calls with only direct passing.
- [Model comparison](model-comparison-findings.md): historical 0/12 acceptance, 58/60 calls.
- [Lightweight confidence](lightweight-confidence-findings.md): earlier bounded campaign, 19/32 calls.
- [General runtime v3](general-runtime-v3-validation.md): fake/service checks, rollout history and live limitations, 19/24 calls.
- [Toolkit validation](toolkit-validation.md): artifact/subagent/recovery evidence, 48/50 calls; [API consistency evidence](acceptance-toolkit-api-consistency.json).
- [Original validation](validation.md): initial lifecycle and denial-policy evidence, 16/20 calls.
- [Historical OpenAPI export](openapi.json): retained evidence; the running app serves its current contract at `/openapi.json`.

The earlier pre-foundation broad unpaid service run passed 220 tests with 2 paid skips **before final payload compaction**. Subsequent checks covered 40 affected tests and four captured SDK/backend regressions. Final SDK/backend preflight passed 15/15 fake cells plus five cross-model payload-parity checks. These are historical results, not checks rerun during documentation cleanup.

## Local checks

Common unpaid checks (service tests require configured local dependencies):

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run pytest -q --integration
```

Broker-dependent suites run sequentially with isolated schemas/queues and broker 18091. Exact latest focused checks are retained here because the historical findings refer back to README commands:

```bash
uv run pytest -q tests/test_completion_models.py
uv run pytest -q --integration tests/test_completion_models_replay.py
uv run python -m scripts.completion_models --phase completion-models-baseline-v1
uv run python -m scripts.completion_models --phase completion-models-verified-v1
```

## Paid smoke-test preference

The user requests a bounded paid smoke test for model-related implementation changes, after fake/service checks pass. Reuse the existing authorized credential and configured evaluation model. Record an explicit request/output cap, freeze the source, use an independent immutable manifest and ledger, and report actual requests, provider usage, failures, and cleanup. This preference does not make default pytest runs or CI paid, does not require paid calls for documentation-only work, and never reopens a terminal campaign or transfers its unused allowance. Do not treat a fake preflight as a paid model check.

## Campaign restrictions

**Retained terminal campaigns are historical evidence, not startup instructions. New paid checks use independent bounded campaigns under the preference above.** Lightweight, model-comparison, completion-loop and both completion-model phases are terminal. Do not rerun/resubmit terminal cells or spend unused allowances. Post-live model changes use a fresh bounded campaign under the standing preference above; they never resume an earlier campaign.

Never reset, relocate, replace or transfer a ledger to extend a cap. Preserve all retained evidence, downloads, wire captures, source hashes and terminal markers. Earlier ledgers remain separate: `/tmp/agent-runtime-acceptance-budget.sqlite` (16/20), `var/acceptance/toolkits-subagents-v1.sqlite` (48/50), and `var/acceptance/general-runtime-v3.sqlite` (19/24). Do not rerun the whole toolkit live suite; only two original slots remain. Unused slots are not new authorization.

The following completion-model commands were executed once after their strict gates. **Both phases are terminal; these commands must not be used to restart them.**

```bash
uv run python -m scripts.completion_models --phase completion-models-baseline-v1 --live
uv run python -m scripts.completion_models --phase completion-models-verified-v1 --live
```

The retained [baseline evidence](../var/acceptance/completion-models-baseline-v1/live.json) and [verified evidence](../var/acceptance/completion-models-verified-v1/live.json) include one immutable root per model-qualified cell. Reinvocation is refused. Preserve phase ledgers and markers; do not authorize a third phase by changing paths or resetting state. Other historical commands and outcomes remain in the evidence documents linked above.
