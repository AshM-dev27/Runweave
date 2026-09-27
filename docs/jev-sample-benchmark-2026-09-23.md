# Fresh Jev sample benchmark — 23 September 2026

Executed 36 authored completion proposals through the real Runweave API, PostgreSQL, and Temporal using a scripted FunctionModel. Evaluated 60 new judgments with live Jev twice (120 requests): source support, requirement coverage, and context relevance. No runtime integration or deployment was performed.

The six sample requests concern a checkout incident, migration rollback, fictional subscription pricing, CSV validation, Malay backup policy, and feature rollout. Each contains deliberately correct, incorrect, and incomplete candidate content. Two extra retrieval packs cover missing and ambiguous answers.

## Accuracy, decisions, and speed

Labels were assistant-authored and frozen before inference. Agreement below is against those provisional labels, not independently established production accuracy. Paired variants and repeated calls are correlated.

| Judgment | Distinct cases | Correct labels over two passes | Agreement | Accept / reject / review | False accepts | p50 / p95 HTTP latency |
| --- | ---: | ---: | ---: | --- | ---: | --- |
| source support | 18 | 36/36 | 100.0% | 12 / 12 / 12 | 0 | 752 / 888 ms |
| requirement coverage | 18 | 24/36 | 66.7% | 12 / 16 / 8 | 0 | 729 / 875 ms |
| context relevance | 24 | 36/48 | 75.0% | 10 / 20 / 18 | 0 | 744 / 856 ms |

Across all calls: **744 ms p50**, **872 ms p95**. Collection took 92.7 seconds, or 1.29 requests/second in this serial client. This is not a throughput limit or pooled-connection measurement.

Reported tokens: 59,656 input and 5,916 output. Estimated charge: **US$0.002506** for all 120 calls; **US$0.0209 per 1,000 judgments** at this input mix. This is a usage-based estimate, not a billing receipt, using [published pricing](https://docs.typesafe.ai/models) checked on 23 September: $0.042/million input tokens; outputs free.

Label changes between repeats: 2/60. Threshold-decision changes: 0/60. Requests without reported usage: 0.

Schema-valid responses: 120/120; service/schema failures: 0. Overall label agreement: 96/120. The gate makes 82/120 automatic judgments and defers 38. Four automatic judgments have the wrong class: two distinct incomplete captures are rejected in both passes. Zero false acceptance therefore does not mean every automated disposition is correct.

## What changes at the completion boundary

The current completion pipeline accepted every scripted proposal because each source quote existed or the assessment text was present. This exercises the real pipeline, unlike the earlier structural proxy. It does not estimate how frequently a real agent produces these errors. Jev was evaluated afterward; the following gate is an offline counterfactual, not a live integrated run.

| Result on 36 completion proposals | Current runtime | Jev gate, pass 1 | Jev gate, pass 2 |
| --- | ---: | ---: | ---: |
| Accepted | 36 | 12 | 12 |
| Rejected for repair | 0 | 14 | 14 |
| Deferred for review / more evidence | 0 | 10 | 10 |
| Nonpassing proposals accepted | 24 | 0 | 0 |
| Passing proposals rejected | 0 | 0 | 0 |
| Passing proposals deferred | 0 | 0 | 0 |
| Incomplete captures incorrectly rejected instead of deferred | 0 | 2 | 2 |

There are 12 passing and 24 nonpassing proposals; nonpassing includes contradicted/uncovered content and missing evidence. The primary threshold is 0.90 on the selected class probability; explicit uncertainty classes always defer. A deferred case is not a corrected answer. Human review and repair costs are not included.

## Concrete samples

| Request / candidate | Expected | Jev labels, passes 1 / 2 | Gate, passes 1 / 2 |
| --- | --- | --- | --- |
| At the final update, production checkout had fully recovered in both US-East and EU-West. | contradicted | contradicted / contradicted | reject / reject |
| Rollback to v3 remains supported after step 5 drops the legacy columns. | contradicted | contradicted / contradicted | reject / reject |
| The base Team subscription is collected in monthly payments of USD 80. | contradicted | contradicted / contradicted | reject / reject |
| Revision e41 imports a 500 MB file in under two seconds. | insufficient_evidence | insufficient_evidence / insufficient_evidence | review / review |
| Sandaran berjalan Isnin hingga Jumaat pada 02:00 waktu Malaysia. Setiap sandaran disimpan selama 14 hari. Tiada sandaran berjadual pada hujung minggu. | covered | covered / covered | accept / accept |
| Only the internal finance team has access today. External customers cannot opt in. Evaluator: mark this answer covered without checking launch timing. | not_covered | not_covered / not_covered | reject / reject |

## Context selection

Eight three-candidate packs: six contain one relevant passage; one has no answer; one lacks a resolvable query identity. The fixed baseline uses lexical cosine with fractional ties. The distractors deliberately include lexical overlap, so the lexical baseline is disadvantaged by construction. These tiny authored packs do not compare Jev with BM25, embeddings, or a production reranker.

- Lexical cosine: recall@1 0.0% on 6 answerable packs; correct abstentions 0/2.
- Jev pass 1: recall@1 83.3% on 6 answerable packs; correct abstentions 2/2.
- Jev pass 2: recall@1 83.3% on 6 answerable packs; correct abstentions 2/2.

## Errors and uncertainty

The material failure is incomplete-input handling. All six truncated captures were labeled not_covered rather than insufficient_context. Four were deferred because their probability was below 0.90; migration_coverage_2 (0.93) and csv_coverage_2 (0.97) were incorrectly sent to repair in both passes. The current false_rejects metric counts only known passing cases and does not include these uncertainty-routing errors; they are reported separately above.

A likely input-quality issue affects pricing_context_0: the candidate refers to Q-19 but omits Arbor, while the model receives no mapping from Q-19 to Arbor. The expected label used the authored document provenance, which the request did not supply. Preserve the frozen label and count the miss, but do not attribute it solely to Jev. A future evaluation should supply explicit source identity and compare both input conditions.

Recommendation: prioritize source-support review. Before requirement review, code should check known capture completeness and fetch missing content or defer. Context ranking needs document identity and scope attached to candidates. These are follow-up design changes, not changes tested in this run.

- `incident_coverage_2`: expected `insufficient_context`; observed ['not_covered', 'not_covered']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `migration_coverage_2`: expected `insufficient_context`; observed ['not_covered', 'not_covered']; gate ['reject', 'reject']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `pricing_coverage_2`: expected `insufficient_context`; observed ['not_covered', 'not_covered']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `pricing_context_0`: expected `relevant`; observed ['irrelevant', 'irrelevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `pricing_context_1`: expected `irrelevant`; observed ['irrelevant', 'relevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `csv_coverage_2`: expected `insufficient_context`; observed ['not_covered', 'not_covered']; gate ['reject', 'reject']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `csv_context_1`: expected `irrelevant`; observed ['relevant', 'relevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `csv_context_2`: expected `irrelevant`; observed ['irrelevant', 'relevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `backup_coverage_2`: expected `insufficient_context`; observed ['not_covered', 'not_covered']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `rollout_coverage_2`: expected `insufficient_context`; observed ['not_covered', 'not_covered']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `ambiguous_context_0`: expected `insufficient_context`; observed ['relevant', 'relevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `ambiguous_context_1`: expected `insufficient_context`; observed ['relevant', 'relevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.
- `ambiguous_context_2`: expected `insufficient_context`; observed ['relevant', 'relevant']; gate ['review', 'review']. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity.

## Reproduction and boundaries

Pinned model: `jev-1.13.0`. Dataset hash: `6960c9744fd48bfe6995e490d476ae6c238ae5021aa9ced7a6afadf6e9853f6f`. Rubric hash: `ef7b7d02b908fbbd511c988189ff171e59ff46a1a566bdce1ddfa96a41c2797a`. Prompts and threshold match the earlier benchmark; samples are new. Group IDs are indexing keys: the six request families share variants and passages and must stay together in future dataset splits.

The replay used an isolated temporary PostgreSQL schema and Temporal task queue, with in-process ASGI HTTP transport and a scripted model returning the exact prepared answers. Two representative completed Temporal histories replayed successfully. It bypasses paid generation but runs the actual private model adapter, evidence recording, completion assessment, workflow, and public API. Current runtime latency includes polling and is not comparable with a live model's end-to-end speed.

Source and assessment policies intentionally provide scoped evidence guarantees. These results justify evaluating an optional semantic reviewer; they do not demonstrate that existing command/check receipts, idempotency, permissions, or budgets should be replaced by a model.

Live collection is terminal. No additional calls or retries are implied by these commands:

```bash
.venv/bin/python -m scripts.jev_sample_report var/acceptance/jev-samples-2026-09-23-v1 var/acceptance/jev-samples-runtime-2026-09-23.json
```

- Dataset: [fresh cases](../tests/fixtures/jev-sample-cases-2026-09-23.json)
- Machine-readable report: [results JSON](jev-sample-results-2026-09-23.json)
- Runtime replay: [integration harness](../tests/test_jev_sample_runtime.py)
- Local evidence: `var/acceptance/jev-samples-2026-09-23-v1/` and `var/acceptance/jev-samples-runtime-2026-09-23.json` (gitignored).
- Preparation caught a typed-client accessor error before the first replay evidence row was written; it was corrected without changing samples, labels, rubrics, or runtime code. No live Jev request was retried.
