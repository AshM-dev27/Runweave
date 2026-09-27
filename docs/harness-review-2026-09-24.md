# Harness review — 2026-09-24

Follow-up review of priorities 1–3: completion reliability, context management, and extension interfaces. This is a development validation, not a production-readiness or full Agents API parity claim.

## Corrections

| Area | Reproduced issue and correction |
| --- | --- |
| Review identity | Numeric output now participates in review input and identity; changing a value cannot reuse an earlier passing review. |
| Source review | Only cited required-source evidence is loaded. An unrelated oversized earlier source no longer causes permanent deferral. Latest source selection has explicit sequence ordering. |
| Optional requirements | Tasks with no required criteria receive an overall-outcome judgment instead of an impossible empty review. |
| Unicode and history | Context compaction and serialization agree on UTF-8 byte sizing. Bounded history removes older pairs when multibyte turns exceed the cache limit. |
| Retrieval | Relevant older turns rank ahead of generic recent matches. Session ownership and child isolation are preserved. |
| Tool lifecycle | Custom execution and reconciliation have total timeouts. Concurrent retries respect leases; uncertain writes remain unknown and are never reported as successful. |
| Cancellation | Expired terminal read intents settle without another remote call; writes with lost receipts remain visible as unknown. Late committed receipts can still be retained. |
| Cleanup | Unknown effects, unfinished children, and one backend failure no longer prevent independent sandbox acknowledgments. Cleanup cannot finish until a locked recheck confirms every operation and child is settled. Command completion uses this same check. |
| Policy boundaries | Exact denied effect scopes are checked before execution; built-in alias collisions fail at installation. MCP arguments are revalidated after runtime idempotency-key injection. |
| Model leases | Model leases cover capacity waiting plus provider timeout; semantic review also has an overall timeout. |

## Scenario coverage

The new audit and concurrency tests cover numeric review changes, uncited long documents, optional-only criteria, Unicode inputs, multibyte history, absent observations, older relevant turns among generic matches, interrupted reads/writes, custom-tool timeouts, removed or changed MCP tools, malformed/NaN/oversized results, cleanup acknowledgment races, concurrent read/write retries, duplicate reviewers, and cancellation during review. Existing tests additionally cover approvals, denial, credentials, revision-bound evidence, actual HTTP MCP execution, isolated sandbox commands, delegation, PostgreSQL recovery, Temporal replay, and public contracts.

The focused run passed **56 tests in 42.23 seconds**. The complete unpaid service suite then passed **297 tests with 4 paid skips in 567.27 seconds**, including both four-case fake acceptance matrices. Ruff checks and format checks, `git diff --check`, generated OpenAPI (32 paths, 44 schemas), and frozen offline dependency validation all passed. All default tests prohibit paid model requests.

## Fresh live evaluation

Campaign `harness-review-2026-09-24-v1` repeats the original four scenarios and the same limits: `openai/gpt-5.6-luna`, reasoning `none`, 16 physical requests total, 1,024 output tokens per request. Review cases allow one request each, context four, and parallel ten, with six for the root and three per child. The runtime root model allowance remains twelve and its token limit is unchanged.

The first two scenarios use scripted candidate actions and a live reviewer; context recall and parallel work use live model decisions. Fake preflight must pass against identical source hashes before live execution. A new immutable ledger, root admissions, terminal latches and exact official-endpoint guard keep this evaluation separate from all historical campaigns. Only synthetic fixture content from a fresh PostgreSQL schema is sent to the provider; credentials are never recorded in results. The previous 3/4 campaign remains preserved and terminal.

**3/4 live scenarios passed using 13/16 physical requests**, all successful HTTP responses. All four root workflow histories replayed. Source hashes stayed unchanged throughout and still match the final implementation. The campaign is terminal and was not resubmitted. [Machine-readable results and source hashes](harness-review-results-2026-09-24.json).

| Scenario | Result | Requests |
| --- | --- | ---: |
| Supported answer | Pass: live reviewer accepted and the task completed | 1 |
| Contradicted answer | Pass: live reviewer returned repair and prevented completion | 1 |
| Earlier conversation | Pass: exact archive-key recall | 1 |
| Parallel work | Fail: both children, merges and file checks succeeded; final parent acceptance was not reached | 10 |

The parallel root made six calls: assign, join, merge left, merge right, verify left, verify right. Both children used two calls. Unlike the previous trace, the root performed no duplicate writes and successfully merged both outputs. It did not use the shorter completion path that automatically executes pending checks, and had no evaluation allocation left for a final completion decision. The next request was refused locally by the campaign guard. The runtime retained its conservative reservation; its retry reported `model_capacity_pending`. The root ledger reported 10,405 tokens and 3,921 reserved tokens against 16,000. This demonstrates a remaining completion-efficiency problem under the evaluation budget; it does not prove that the runtime token limit alone caused the failure or that increasing the budget would guarantee success.

The full-suite pass and live result apply to the same final implementation. No source correction or additional paid attempt was made after this evaluation. The earlier 3/4 result and historical 11/15 matrix remain separate evidence, not an aggregate reliability score.

## Cleanup

All temporary PostgreSQL schemas returned to the baseline. Eight orphaned workflows created by this review's isolated tests were terminated after their fixture schemas were removed; workflow history is retained. No pytest process or sandbox job container remained. The dedicated test broker was stopped while preserving its container and state volume. Production container identities stayed unchanged.

Temporary test directories and generated Python/pytest/Ruff caches were removed. Raw test logs, source hashes, immutable request ledger, preflight, results, and cleanup verification are retained in `var/acceptance/harness-review-2026-09-24-v1/`. Checksums confirm that the original foundation evidence is unchanged. Unrelated workspace files and retained campaigns were preserved. Restart the test broker with `sudo docker start agent-runtime-v3-test-broker` before future broker-dependent tests.

## Limits

Review confidence remains uncalibrated, retrieval is lexical, and operators install trusted extensions. Unknown external effects intentionally keep cleanup pending until reconciliation; no public post-terminal reconciliation endpoint is provided. Custom backend interfaces do not supply additional sandbox implementations. A four-case, single-model smoke test cannot establish broad autonomous reliability or full Agents API parity.
