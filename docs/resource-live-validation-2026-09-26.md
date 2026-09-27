# Paid resource validation — 2026-09-26

Paid checks exposed repeated work that fake models did not reproduce. After improving the model's view of completed actions, both targeted follow-up scenarios passed within their existing limits. The user's [standing preference](validation-index.md#paid-smoke-test-preference) is now to include bounded paid smoke checks for model-related implementation changes.

## Results and corrections

The initial campaign passed **2/4 scenarios**, spending **17/17 physical requests**. The subsequent correction campaign passed **2/2 targeted scenarios**, spending **14/15 requests**. Both used the existing `gpt-5.6-luna` configuration, reasoning `none`, and a 1,024-token output cap. Source hashes stayed unchanged during each campaign. All 31 requests returned successful HTTP responses; HTTP success alone did not establish task acceptance.

| Scenario | Initial paid outcome | Follow-up |
| --- | --- | --- |
| Model pause/update/resume | Repeated `add(5,7)` despite receiving 12; exhausted three attempts and timed out while paused. | Completed with two model calls and exactly one tool effect. Authenticated increase and idempotent update retry succeeded. |
| Reviewer accepts correct answer | Passed after resource pause/resume. Paid reviewer accepted the fake actor's answer 12. | Not repeated; reviewer code unchanged. |
| Reviewer rejects incorrect answer | Passed after resource pause/resume. Paid reviewer returned `repair` for answer 13. The scripted actor then stopped with expected `task_blocked`. | Not repeated; reviewer code unchanged. |
| Shared parallel children and parent completion | Both children completed and exact files were integrated, but repeated joining and individual checks consumed capacity before final acceptance. | Both children and parent completed; final verification accepted the integrated revision. |

The compact action history now includes execution status and the `add` inputs alongside its successful result. Instructions explain that recorded actions already executed, merged children are already integrated, and proposing completion runs pending checks. This gives the actor clearer evidence to reuse. It adds no permission decision, model call, or action restriction. A deterministic regression covers result attribution and completion without repeating the tool.

The parallel scenario kept the same request limits: 12 across the tree, at most 8 for the parent and 3 per child. Its runtime token limit stayed at 16,000. Each child had an allocation estimate of one model attempt; both campaigns completed children using **2 and 3 attempts**, demonstrating shared allocation beyond estimates. The follow-up used 7 parent calls and 5 child calls, produced exactly `left.txt = "12\n"` and `right.txt = "20\n"`, and reached final acceptance. The parent still performed individual file checks, so further efficiency improvement remains possible.

In the first parallel run, 12,585 reported tokens left 3,415 available, below the next 3,981-token reservation estimate. Its pause was consistent with the configured token limit. It was cancelled for cleanup. The correction did not increase that limit or change admission accounting.

## Actual provider usage

| Campaign | Requests / cap | Input tokens | Output tokens | Total tokens |
| --- | ---: | ---: | ---: | ---: |
| `resources-live-2026-09-26-v1` | 17 / 17 | 15,306 | 793 | 16,099 |
| `resources-completion-2026-09-26-v1` | 14 / 15 | 13,695 | 768 | 14,463 |
| Total actual usage | 31 | 29,001 | 1,561 | 30,562 |

These are provider-reported usage values, not dollar estimates. Runtime accounting also includes fake actor usage in the review fixtures, so it is not identical to provider billing. Each campaign has its own immutable manifest, admissions, ledger and terminal markers. No earlier allowance was reset, reopened or transferred; the remaining follow-up allowance is not reusable.

## Final unpaid validation and token comparison

The corrected source passed **246 default tests with 87 skips in 77.06 seconds** and **57 focused tests with one paid test deselected in 57.77 seconds**. The focused run exercised resource policy, API/recovery, model context and completion review against isolated services. These overlapping suites must not be added together. The earlier **323-test full integration baseline** predates this compact-context correction; it is retained separately in the [initial report](resource-policy-validation-2026-09-26.md). Ruff lint, format and `git diff --check` passed.

The payload comparison now includes agent instructions, message content and compact output schemas, using `o200k_base`; it excludes timestamps and provider protocol framing. The initial report's absolute counts omitted common instructions, without changing its legacy/shared differences.

| Fixture | Legacy input | Shared input | Difference |
| --- | ---: | ---: | ---: |
| Direct | 647 | 647 | 0 |
| Workspace read | 758 | 758 | 0 |
| Delegating parent | 1,124 | 1,059 | -65 |
| Child | 792 | 745 | -47 |

These controlled comparisons support no added model payload cost from the resource policy in these fixtures. They do not claim that additional authorized work consumes no tokens.

## Replay, cleanup and limits

Both follow-up root histories replayed. Three initial root histories replayed during the campaign; the cancelled parallel history replayed separately during cleanup. The original failed result remains unchanged, with the later replay recorded in the cleanup evidence.

All isolated test schemas were removed. Three orphaned test workflows were terminated only after confirming their fixture schemas were absent. The dedicated test broker was stopped. No test worker or sandbox job remained. Production services were not restarted, and the changes were not deployed. Task-owned temporary files and caches were removed after preserving logs and source snapshots.

This is a small smoke sample on one model. The reviewer tests deliberately use fake actors and paid reviewers; they do not establish general review accuracy. The two campaigns used different source snapshots and are not a combined acceptance score. Broader reliability and deployment readiness remain open.

[Machine-readable summary](resource-live-results-2026-09-26.json) links the preserved raw evidence, usage, logs, source snapshots and cleanup records.
