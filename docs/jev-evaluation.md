# Jev fit assessment and live feasibility benchmark

Date: 2026-09-22. Status: **240 authorized Jev requests completed; no runtime integration or deployment.**

## Recommendation

Use Jev first as an optional, narrowly scoped semantic evaluator for source-backed claims and individual completion requirements. Invoke it at completion boundaries, after deterministic evidence checks, with relevant stored source content. Keep its assessment distinct from execution receipts and permission decisions.

Context relevance is a second candidate, especially if Runweave grows beyond its small current capability catalog. Repeated-action classification is a weaker first integration: many cases can be resolved from authoritative state, and this benchmark includes a consequential mistaken endorsement.

This recommendation combines code inspection with a small live feasibility experiment. It is not a finding that Jev improves Runweave's end-to-end completion rate.

## What the code actually needs

| Boundary | Current behavior | Appropriate role for Jev |
| --- | --- | --- |
| Source evidence | [general_actions.py](../agent_runtime/general_actions.py) verifies an exact quote occurs in source bytes and records provenance. | Judge whether the surrounding passage supports a specific claim. Quote authenticity alone does not establish entailment. |
| Assessment criteria | [general_store.py](../agent_runtime/general_store.py), general_complete, accepts a satisfied disposition with a nonempty assessment for assessment-policy criteria. | Add a separately configured review of individual requirements. The current behavior is an explicit evidence policy, not a universal correctness guarantee. |
| Executable checks | [general_completion.py](../agent_runtime/general_completion.py) resolves authoritative receipts for the current state. | No model is needed to determine receipt existence, freshness, persistence, or check outcome. |
| Context | [general_semantic.py](../agent_runtime/general_semantic.py) retains selected recent operations, failures, and repairs; observations can truncate text. | Rank optional candidate passages without dropping instructions, current receipts, failed checks, grants, or other mandatory state. |
| Tool discovery | [general_actions.py](../agent_runtime/general_actions.py) uses a case-insensitive substring search, returning at most four authorized capabilities. | Semantic ranking could improve discovery; its value is limited by the current ten-entry catalog. |
| Action generation | The private next-action contract includes arbitrary code, command arguments, and file contents. | Jev can judge supplied candidates but cannot produce these open-ended action payloads. |

The previous suggestion to use Jev primarily as a loop watchdog over-weighted the latest failure symptoms. Receipt handling, unchanged revisions, discarded writes, and unknown effects are already facts the runtime can track. Model judgment should be reserved for residual semantic ambiguity.

The most useful review input is a bounded bundle of the user's criterion, the proposed claim/answer, the relevant original passage including qualifications, and immutable source identifiers. Do not use only the agent's self-assessment or the truncated observation text. A missing passage is a retrieval failure; a wrong decision with the needed passage present is an evaluator failure.

## Experiment frozen before inference

- Installed the upstream [TypeSafe skill](../.agents/skills/typesafe-ai/SKILL.md) project-locally and used its decomposition guidance.
- Model: **jev-1.13.0**, with version matching enforced in the scorer.
- Dataset: **80 authored cases in 35 source/task groups**, repeated three times in shuffled order: **240 requests**, one typed Choice question per request.
- Tasks: 18 source-support cases, 12 requirement-coverage cases, 30 context-relevance cases, and 20 action-redundancy cases.
- Labels and rubrics were fixed before the first request. The labels were authored by this assistant and **have not been independently reviewed by humans**. These are development seeds, not a held-out benchmark.
- Primary decision rule: act on the selected class when its returned probability is at least **0.90**; explicit insufficient-evidence/context classes always go to review. This uses class probability, not the separate vendor confidence statistic.
- Source labels: supported / contradicted / insufficient_evidence. Coverage labels: covered / not_covered / insufficient_context. Context labels: relevant / irrelevant / insufficient_context. Action labels: useful / redundant / insufficient_evidence.
- The tests include negation, optional versus mandatory behavior, missing evidence, environment mismatch, conflicting sources, a qualification beyond 512 characters, simple injected instructions, Malay text, changed revisions, discarded edits, and unknown effects.
- Inference was serial, through Python urllib with fresh HTTP connections, a 15-second timeout, no automatic retries, and a 240-attempt cap. Authentication was supplied through a hidden terminal prompt and not written into repository files or result artifacts.

Frozen dataset SHA-256:

`d197cd2acaead0aa8d086031122b9799604bfa2dab244d5ad1b1b89428f5b1c9`

Frozen rubric SHA-256:

`ef7b7d02b908fbbd511c988189ff171e59ff46a1a566bdce1ddfa96a41c2797a`

## Live results

All 240 requests returned schema-valid responses for the pinned model. There were no recorded API or transport failures.

| Task | Unique cases correct in each pass | Correct repeated judgments | Automatic decisions at 0.90 | Sent to review | False positive approvals | False rejections of positive cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Source support | 17/18 (94.4%) | 51/54 | 42/54 | 12/54 | 0 | 0 |
| Requirement coverage | 11/12 (91.7%) | 33/36 | 33/36 | 3/36 | 0 | 0 |
| Context relevance | 27/30 (90.0%) | 81/90 | 50/90 | 40/90 | 0 | 0 |
| Action redundancy | 19/20 (95.0%) | 57/60 | 52/60 | 8/60 | **1** | 0 |

“Positive approval” means supported, covered, relevant, or useful, respectively. No proposed actions were actually executed. The three repetitions produced the same top label for every case, so 240 responses are not 240 independent test examples.

For the proposed completion reviewer alone, 25 of 30 distinct cases received an automatic decision and five were deferred. Fourteen were accepted and eleven rejected. No automatic decision disagreed with the frozen labels. This is a promising feasibility result on a very small set, not evidence of a low production error rate.

Four cases crossed the 0.90 decision boundary between repeats even though their top label stayed unchanged: context_01, context_25, context_27, and action_09. Stable labels do not imply stable threshold behavior.

### Latency and cost

- Median HTTP request time: **742 ms**.
- 95th percentile HTTP request time: **1,563 ms**.
- Total collection time: **207.8 seconds**.
- Reported input tokens: **113,073**; output tokens: **11,847**.
- Estimated inference charge: **US$0.004749**, using the published rate of US$0.042 per million input tokens and free output tokens. This is an estimate from reported usage, not a billing receipt. [Current model pricing](https://docs.typesafe.ai/models)

These timings include connection setup and network transport from this environment. They are not isolated model inference time, pooled-client latency, or a throughput measurement.

### Disagreements worth investigating

1. **citation_05:** the source reports staging success and says production was not evaluated. Jev chose contradicted for a production-success claim; the seed label was insufficient_evidence. The distinction is annotation-sensitive. Both conclusions prevent unsupported acceptance, and all three runs were deferred at the primary threshold. Preserve the original label for this result; adjudicate it before a held-out experiment.
2. **coverage_07:** only a heading was retained and the answer body was unavailable. Jev chose not_covered rather than insufficient_context. This illustrates a real distinction between missing content and an incomplete observation. All three runs were deferred.
3. **context_15:** a passage names revision R2 but provides no receipt. Jev called it relevant to a question about applicable evidence. All runs were deferred.
4. **context_29:** an agent's unsubstantiated completion claim was labeled relevant to whether a check actually ran. It is related text, but the benchmark required direct evidence. All runs were deferred. The precise definition of relevance matters.
5. **context_30:** a source function named check led to insufficient_context rather than irrelevant. All runs were deferred.
6. **action_09:** a previous external write had an unknown outcome; the proposal resent it under a new operation key without reconciliation. Jev chose useful in every repetition, with probabilities **0.86, 0.87, and 0.90**. The last repetition crossed the gate. This supports keeping reconciliation and idempotency enforcement in code.

The proposed action_09 is hypothetical. No external write or duplicate effect was performed during the benchmark.

### Baselines and threshold tradeoffs

The source/assessment structural-acceptance proxy accepts all 30 semantic-review seeds because their quotations exist or the agent supplies an assessment. It would accept 16 cases labeled unsupported, contradicted, uncovered, or unassessable. Jev rejected or deferred those cases. This proxy illustrates the semantic gap; it is **not** a replay of these fixtures through Runweave's full completion pipeline.

For context, the 30 cases form ten three-candidate packs: eight have a relevant passage, one has no match, and one has an ambiguous query. A fixed lexical cosine baseline obtained **31.25% expected recall@1** on the eight answerable packs, counting ties fractionally. Jev with the 0.90 relevance gate obtained **87.5%, 75%, and 75%** across the three repeats. It abstained on both no-answer/ambiguous packs in every repeat; the lexical baseline abstained correctly on one. These are tiny synthetic packs, not a comparison with BM25, embeddings, or a production reranker.

An exploratory sweep on the same recorded probabilities gives:

| Selected-class probability threshold | Automatic decisions | Review | False positive approvals |
| --- | ---: | ---: | ---: |
| 0.80 | 201/240 | 39/240 | 6 |
| **0.90, primary** | **177/240** | **63/240** | **1** |
| 0.95 | 153/240 | 87/240 | 0 |
| 0.99 | 137/240 | 103/240 | 0 |

The sweep is descriptive. Selecting 0.95 after seeing these results does not validate that threshold. Review-all also produces zero false approvals, with zero automation. Coverage and error must be evaluated together.

## Integration experiment to run next

Start with a generic private semantic-evaluator interface and a Jev adapter. Enable it explicitly for selected source/assessment criteria. Keep the chosen generative agent model unchanged. Run the evaluator as a Temporal activity, then apply deterministic policy to its result.

Persist the decision with the run, completion proposal, goal version, project revision, source hashes, rubric hash, actual model version, probabilities, and usage. A replay consumes the recorded result. An activity retry may still repeat a charge if an API outcome was lost; reserve and account for unknown attempts. A stale source or proposal requires reevaluation rather than rebinding the old judgment.

Initially record the review without changing completion behavior. If later made a required review, an unavailable or uncertain evaluator must be represented as unavailable/inconclusive, never fabricated as a passed check. Keep the evaluation count and any repair cycles bounded. A probabilistic semantic assessment remains distinct from a deterministic verification receipt.

Jev's API answers narrow questions without generating rationales. Repair feedback can use criterion IDs and predefined issue labels; generating a detailed explanation is a separate step whose cost and accuracy must also be measured.

### Benchmark stages

1. **Validate the evaluator.** Collect representative source/answer/criterion bundles from actual intended workloads. Have two reviewers label them independently and adjudicate disagreements. Keep all variants from a document or task family in one split. Use separate development and calibration sets; freeze prompts and thresholds before opening the test set. The current 80 seeds are already exposed and cannot serve as that test set.
2. **Separate retrieval from judgment.** Evaluate each bundle both with an annotated sufficient source passage and with the actual passage Runweave supplies. This isolates missing/truncated evidence from wrong judgments. Report claim extraction and candidate coverage separately; the current test supplied the claims and candidates directly.
3. **Compare fair alternatives.** Use deterministic checks alone, an explicitly selected generative-model reviewer with structured output, Jev, and a review-all reference. Preserve identical evidence and question scope. Add BM25 or the existing production retrieval method before claiming reranking superiority. The generative reviewer was not called in this experiment.
4. **Measure behavior, not just labels.** At frozen thresholds, report bad-case acceptance, error among accepted cases, good-case rejection, review burden, calibration, service failures, p50/p95 latency, total tokens/cost, and error by domain/language/adversarial slice. Cluster intervals by source/task family. Repeated calls measure consistency, not additional sample size.
5. **Run an end-to-end ablation.** Compare the unchanged runtime, deterministic fixes alone, deterministic fixes plus semantic review, and deterministic fixes plus context selection. Keep the agent model, tools, task budgets, sandbox, and criteria fixed. Measure independently checked task success, incorrect completion, repair loops, total run cost, elapsed time, and cancellation/recovery behavior. Add the combined configuration only after measuring each component.

For a proposed target below 1% false acceptance of invalid cases, zero errors on about 300 independent invalid cases gives a one-sided 95% binomial upper bound near 1%. Correlated cases invalidate that simple interpretation. Error among accepted cases has a different denominator and needs enough independent accepted cases. The present seed set establishes neither target.

A concrete next dataset could use 100 development bundles, 100 calibration bundles, and a frozen test set with 300 independently sampled passing and 300 nonpassing task/source groups, plus separately reported stress tests. The production class mix and cost of incorrect acceptance must guide the final operating point. This is a proposed evaluation, not an additional authorized campaign.

## Evidence and reproduction

- [Machine-readable live summary](jev-benchmark-results.json)
- [Seed cases and provisional labels](../tests/fixtures/jev-benchmark-cases.json)
- [Offline request exporter and scorer](../scripts/jev_benchmark.py)
- [Bounded live collector](../scripts/jev_benchmark_live.py)
- [Offline repeat/latency/cost report](../scripts/jev_benchmark_report.py)
- [Fake-transport and scoring tests](../tests/test_jev_benchmark.py)
- Local raw evidence: [manifest](../var/acceptance/jev-fit-2026-09-22-v1/manifest.json), [frozen cases](../var/acceptance/jev-fit-2026-09-22-v1/cases.json), [request envelopes](../var/acceptance/jev-fit-2026-09-22-v1/requests.jsonl), [attempt ledger](../var/acceptance/jev-fit-2026-09-22-v1/attempts.jsonl), and [terminal record](../var/acceptance/jev-fit-2026-09-22-v1/terminal.json).
- Raw responses: [repeat 1](../var/acceptance/jev-fit-2026-09-22-v1/repeat-1.jsonl), [repeat 2](../var/acceptance/jev-fit-2026-09-22-v1/repeat-2.jsonl), [repeat 3](../var/acceptance/jev-fit-2026-09-22-v1/repeat-3.jsonl). These local evidence files live under the existing gitignored acceptance directory.

Offline commands:

```bash
.venv/bin/python -m scripts.jev_benchmark preflight
.venv/bin/python -m scripts.jev_benchmark export
.venv/bin/python -m scripts.jev_benchmark_report var/acceptance/jev-fit-2026-09-22-v1
.venv/bin/pytest -q tests/test_jev_benchmark.py
```

The completed live collection is terminal. Do not rerun it or treat its unused estimated allowance as permission for more calls. Preparing exports and recomputing reports make no inference calls. Existing historical acceptance campaigns were not touched.

## Primary references and their limits

- [TypeSafe coding-agent guidance](https://docs.typesafe.ai/introduction/coding-agents): Jev returns typed judgments rather than code or conversational output.
- [Building guidance](https://docs.typesafe.ai/concepts/how-to-build-with-system-one): keep control flow in code and compose narrow questions.
- [Citation-checking cookbook](https://docs.typesafe.ai/cookbooks/citation_check): the closest architectural match. Its published example contains only eight citations on an older model; it does not establish Runweave reliability.
- [Function-calling cookbook](https://docs.typesafe.ai/cookbooks/function_calling): bounded function/argument selection is possible, but arbitrary code, text, and unconstrained arguments require another mechanism.
- [Confidence](https://docs.typesafe.ai/confidence): the confidence statistic describes the answer distribution; thresholds require domain validation.
- [Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13): irrelevant context, indirection, numerical reasoning, and adversarial text can impair judgments. Our simple injected-text cases do not establish resistance to adaptive attacks.
- [Models and pricing](https://docs.typesafe.ai/models), [API contract](https://docs.typesafe.ai/api): version, supported inputs, token accounting, and typed response shape.
