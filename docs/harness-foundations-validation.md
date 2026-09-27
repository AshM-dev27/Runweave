# Harness foundation validation — 2026-09-23

This records the implementation of completion reliability, context management, and extension interfaces described in the [operator guide](harness-foundations.md). It is a development validation, not a production-readiness or full Agents API parity claim.

## Unpaid coverage

The complete unpaid service run (`pytest -q --integration`) passed **268 tests with 3 paid skips** in 548.43 seconds. Two subsequently added focused regressions for receipt reuse and the immutable live ledger also passed. Ruff checks, format checks, `git diff --check`, public OpenAPI generation, and an offline frozen dependency dry run passed.

The default suite disables real model requests. New tests cover:

- Review pass/repair/defer, low confidence, unavailable models, multiple outputs, changed-answer invalidation, retry reuse, complete source context, oversized-source deferral, and deterministic checks before paid review.
- Explicit pending checks while preserving genuine uncertainty.
- Context compaction, exact retrieval beyond the bounded adapter cache, session/child isolation, and legacy registration identities.
- Pinned tool/skill definitions, capability denial, argument validation, credential redaction, upstream schema checks, approval, ambiguous writes, reconciliation and exhausted recovery.
- Actual PostgreSQL/Temporal completion repair, external-write approval/retry, HTTP MCP execution, cancellation races and workflow replay.

The isolated service tests use their own database schemas and Temporal queues plus the test broker on port 18091. No production services are restarted. Existing recovery/replay and contract tests are included in the broad unpaid run.

## Bounded live evaluation

The user authorized live tests on 2026-09-23. Campaign `harness-foundations-2026-09-23-v1` is separate from every retained campaign. It uses the existing registered `openai/gpt-5.6-luna` model with reasoning `none`, a maximum of 16 physical requests, and 1,024 output tokens per request. Limits include transport attempts and per-scenario allocations; root admissions and terminal latches prohibit resubmission. Provider HTTP success alone is not a passing task result.

The four scenarios are:

| Scenario | What passes |
| --- | --- |
| Supported answer | A live semantic reviewer accepts the source-supported candidate; durable task completion and replay succeed. |
| Contradicted answer | A live semantic reviewer rejects the intentionally unsupported candidate; it cannot complete. |
| Earlier context | A live agent recalls the exact key from an earlier completed turn beyond the framework cache. |
| Parallel work | A live root delegates to two live children, merges exact file outputs, passes integrated checks, completes, and replays. |

The first two use scripted main-agent actions to isolate the live reviewer. The latter two use live autonomous task decisions. A fake-model rehearsal exercises the same API/activity/workflow paths before the live run and records the tested source hashes. The source snapshot must stay unchanged during live execution.

The detailed local evidence is retained under `var/acceptance/harness-foundations-2026-09-23-v1/`: preflight report, source hashes, immutable SQLite request ledger, and operations/results report. Do not reset, relocate, or reuse its ledger to extend the cap. Prior terminal campaigns and their unused allowances remain untouched.

## Recorded live result

**3/4 scenarios passed; 13/16 physical requests used**, all with successful HTTP responses. The source remained unchanged during the live run, and all four workflow histories replayed. The campaign is terminal. [Machine-readable summary and source hashes](harness-foundations-results-2026-09-23.json).

| Scenario | Result | Physical requests |
| --- | --- | ---: |
| Supported answer review | Pass: accepted and completed | 1 |
| Contradicted answer review | Pass: `repair` verdict prevented completion | 1 |
| Earlier conversation context | Pass: exact `AMBER-91` recall | 1 |
| Parallel work | **Fail:** children completed and bytes matched, but parent did not reach accepted completion | 10 |

The parallel parent used six requests and the children used two each. The parent recreated delegated output, joined, merged one child, repeated an unchanged write, and verified one file. Its next request was refused by the campaign's allocation. The runtime conservatively retained that reservation; the retry then reported `model_capacity_pending`. The tree had reported 10,075 tokens and retained 3,841 reserved tokens. This failure does not prove that the 16,000-token runtime limit alone prevented completion, or that a larger allowance would produce success.

The trace exposed missing merge-progress context. The subsequent offline correction projects successful merges from durable receipts, supplies the next join/merge action for outstanding children, and explains unchanged writes. A regression reproduces the observed redundant writes, then verifies integration of both children, automatic checks, accepted completion, exact bytes and replay. The post-live focused run passed **46 tests in 68.15 seconds**, covering the new regression plus semantic/receipt contracts, real-service repair, extension execution and workflow replay. Ruff and diff checks also passed. This correction changes the source after the live snapshot and has **not** been revalidated with a live model. The 3/4 result describes the frozen pre-correction source only.

The [2026-09-24 follow-up review](harness-review-2026-09-24.md) subsequently tested the corrected implementation with 297 unpaid tests and a separate live campaign. The results above remain the original source snapshot.

## Limits

A four-case single-model evaluation is only a smoke test. Review confidence is uncalibrated, context retrieval is lexical, and extensions are installed by trusted operators. An MCP write's idempotency declaration depends on the actual upstream contract. Unresolved external effects remain observable and blocking; there is no public post-terminal reconciliation endpoint. The bundled executor still supports the bounded offline Python project environment. These results cannot be combined with the historical 11/15 matrix into a new acceptance percentage.
