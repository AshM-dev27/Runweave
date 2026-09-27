# Jev value decision for Runweave — 23 September 2026

**Decision: do not add Jev to the default harness on the current evidence.** Its low inference price and ability to catch planted answer errors do not establish an improvement to the current coding harness. The measured configuration adds review friction to valid answers, and the observed loop failures have deterministic signals.

## What was tested

- Inspected the latest retained 15 real generative-agent fixture runs: 11 accepted, four failed. All required criteria used executable-check policies. These are historical runs; no original paid campaign was restarted.
- Evaluated all 32 captured write proposals, including 13 exact duplicates, against pre-action persisted bytes. A SHA-256 comparison was validated independently against the recorded post-action changed flags.
- Reviewed 11 unmodified successful agent answers against their actual tiny fixture files, preservation facts, and check/command outcomes. Expected source-support labels were adjudicated by this assistant before inference.
- Added four variants per answer: one contradictory statement, one unverified performance claim, missing evidence, and unrelated/injected log text. Variants are capability stress tests, not naturally occurring production defects.
- Ran 87 cases twice: **174 fresh Jev requests**, model `jev-1.13.0`, fixed selected-class probability threshold **0.90**, no automatic retries. Uncertain labels always defer.
- Re-ran **26 focused deterministic recovery/Temporal replay tests** and **16 offline scorer/precheck tests**: all passed. No runtime code was changed.

## Incremental value versus code

| Boundary | Deterministic/current harness result | Jev result in each pass | Value decision |
| --- | --- | --- | --- |
| Retained valid final answers | 11/11 accepted; file/check oracles pass | 6/11 and 6/11 accepted; five valid answers deferred each time | No new natural defect caught; adds review burden |
| Pure file replacements | 32/32 predictions match actual effects: 13 duplicates, 19 changes | 13 duplicates rejected, 16 changes accepted, three changes deferred | Prefer exact byte comparison |
| Known missing evidence | An explicit incomplete flag can defer 11/11 without a model | 11/11 deferred in both passes | No semantic judgment needed to detect known absence |
| Four failed real runs | Already rejected: repeated writes/no progress and reservation/check/child issues | No final successful answer exists to review | No demonstrated task-success improvement |

The six-of-eleven result is an offline application of the Jev gate to retained successful outputs. It is not a new measured end-to-end success rate of 6/15. No human completed the reviews and no live repair loop was run. A deferred answer is not a corrected answer.

The duplicate-write comparator uses a known current revision and exact expected content hashes. It must remain bound to current authoritative state. It is a benchmark helper, not a new runtime authorization rule. Finding a duplicate after proposal does not recover the already-spent generative call or prove that a feedback loop will complete successfully.

## Detailed Jev results

| Slice | Distinct cases | Label agreement, pass 1 / 2 | Accept / reject / review, pass 1 | Accept / reject / review, pass 2 |
| --- | ---: | --- | --- | --- |
| natural | 11 | 100.0% / 90.9% | 6 / 0 / 5 | 6 / 0 / 5 |
| contradiction | 11 | 100.0% / 100.0% | 0 / 11 / 0 | 0 / 10 / 1 |
| unverified | 11 | 90.9% / 100.0% | 0 / 0 / 11 | 0 / 0 / 11 |
| missing | 11 | 90.9% / 100.0% | 0 / 0 / 11 | 0 / 0 / 11 |
| noisy | 11 | 100.0% / 90.9% | 6 / 0 / 5 | 6 / 0 / 5 |
| captured_write | 32 | 100.0% / 100.0% | 16 / 13 / 3 | 16 / 13 / 3 |

No planted contradictory or unsupported answer was accepted. Contradiction variants were rejected 11/11 in pass one and 10/11 in pass two, with the remaining case deferred. All added unverified performance claims were deferred. This demonstrates useful discrimination on planted faults, but does not establish how often such faults occur in Runweave or whether review improves total task outcomes.

The five valid-answer deferrals persisted in both passes. One correct bug-fix answer also changed its top label from supported to contradicted at low probability. The gate prevented an automatic false rejection, but still requires someone or another model to review an already-correct result. Neither review labor nor downstream model cost is included in inference pricing.

## Speed and cost

- Jev HTTP latency: **729 ms median**, **841 ms p95**. One question per request, serial fresh connections; includes network overhead.
- Inference estimate: **US$0.005375** for 174 calls; 127,974 input tokens and 8,772 output tokens. Uses [published pricing](https://docs.typesafe.ai/models) of $0.042/million input tokens and free outputs, checked on 23 September; not a billing receipt.
- Exact duplicate comparison: **1.14 microseconds per write** in a local CPU microbenchmark after state is available; zero model/API cost. This excludes database lookup and is not an end-to-end service timing.
- All 174 requests returned valid responses. Label changes: four cases; gate changes: one planted-contradiction case. The test does not measure concurrent throughput or SDK connection pooling.

## Adoption decision

The predeclared rule required incremental benefit over deterministic checks on representative natural errors, preservation of valid completion, and an acceptable measured end-to-end tradeoff. It is not satisfied. Current evidence supports improving deterministic progress feedback, authoritative check handling, and reservation headroom first. Those runtime changes still require their own implementation and validation; this experiment did not apply them.

Retain Jev as an unintegrated candidate for a future document/source-grounded workload where semantic errors actually occur and executable checks cannot resolve them. Before adoption, compare the unchanged harness, deterministic improvements alone, and deterministic improvements plus Jev on fresh real tasks with independently reviewed outcomes. Measure accepted incorrect results, completed correct tasks, unnecessary repairs/reviews, and full run time/cost. Do not count planted-error detection or low per-call pricing alone as an adoption win.

Potential follow-ups such as splitting whole answers into individual claims, clearly separating fresh from historical receipts, or using a different threshold may improve this configuration. They were not tested and cannot be claimed as current benefits. Do not tune the threshold on these same cases and call that independent validation.

## Evidence, privacy, and limitations

Frozen external dataset hash: `4f1c253c4ea88bfcefd07edee10579c84103e55e9037e4aac66ea2e1ac01c455`. Original evidence files and downloaded fixtures were verified byte-identical after testing. There are five original task families, not 87 independent tasks or 174 independent samples.

Automatic approval review initially blocked a proposed transfer of retained trace-derived data. Before any call, every candidate payload was audited: its contents were only synthetic arithmetic/CSV fixtures and short generated answers. The payload was minimized to remove UUIDs, revision hashes, raw logs, internal paths, and source-model identities. The safer payload passed approval review. No raw trace or credential was sent in the evaluation state; credentials were supplied through the collector's hidden prompt.

- [Machine-readable results and case-level judgments](jev-harness-value-results-2026-09-23.json)
- [Dataset preparation](../scripts/jev_harness_value.py), [payload minimization](../scripts/jev_harness_payload.py), [offline report](../scripts/jev_harness_report.py)
- Local raw evidence, frozen adoption protocol, payload audit, and JUnit report: `var/acceptance/jev-harness-value-2026-09-23/` (gitignored).
- Recorded source of the real agent runs: [completion model findings](completion-models-findings.md).
- The live collection is terminal. The report can be recomputed offline with `.venv/bin/python -m scripts.jev_harness_report`; this makes no paid calls.
