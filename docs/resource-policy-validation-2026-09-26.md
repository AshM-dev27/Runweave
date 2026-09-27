# Resource policy validation — 2026-09-26

This change adds opt-in shared resource allocation and durable budget waits without additional model permission instructions. Configuration and limits are documented in [resource policy](resource-policy.md).

This report records the initial unpaid source snapshot. Subsequent paid testing found completion inefficiencies, prompted a compact-context correction, and passed both targeted follow-up scenarios. See the separate [paid validation report](resource-live-validation-2026-09-26.md); the results below remain historical evidence.

## Results

The final full unpaid suite passed **323 tests with 4 paid skips in 625.34 seconds**. Implementation and test hashes stayed unchanged throughout that run. Ruff lint/format and `git diff --check` also pass. See [machine-readable results](resource-policy-results-2026-09-26.json).

The final focused run passed **29 checks**: 26 resource scenarios plus three immutable evaluation-guard checks. Eleven transport/guard tests also passed after updating assertions to require the typed non-dispatch result.

The first broad run found five tests matching the previous free-text guard exception and one acceptance preflight that detected source changes during execution. The transport assertions now require `RequestNotDispatched` and retain their request-count, admission and failure-latch checks. The acceptance preflight passed with stable source. The second complete run passed against the frozen final implementation and test snapshot.

## Covered scenarios

- Shared child estimates can be exceeded within actual root capacity; fixed local limits remain enforceable and can be increased within their declared ceilings.
- Twelve concurrent charge attempts against three available calls admit exactly three.
- Authentication, strict input validation, pinned operator/provider ceilings, terminal fencing, version conflicts, monotonic increases and idempotent update retries.
- Known unsent requests release reservations once, including SDK wrapping; generic timeouts and misleading exception text retain uncertain reservations.
- A capacity wait releases after settlement. Stale waiters and unrelated limit increases cannot clear a newer pause.
- Active-time limits pause; approval/budget waits do not double-count overlapping time.
- An in-flight response survives a concurrent tree pause. Saved completion proposals resume without another actor model request.
- Effect approval can resolve during a budget pause; the authorized write still commits once.
- Temporal pause/resume at model, tool and review boundaries; cancellation, explicit fail mode, pause timeout and deterministic replay.
- Enlarging shared capacity does not grant an unauthorized installed capability or a child budget-update endpoint.

## Token and model-call evidence

All checks in this initial phase used fake models. **No paid model requests were made in this phase.** Four deterministic comparisons count model-visible message content and the compact output schema with `o200k_base`. Internal message timestamps are excluded. This original measurement omitted the common agent instructions; its legacy/shared differences remain valid. The subsequent [paid validation report](resource-live-validation-2026-09-26.md) and current guide include those instructions in the absolute input counts.

| Fixture | Legacy | Shared | Added tokens |
| --- | ---: | ---: | ---: |
| Direct task | 456 | 456 | 0 |
| Workspace read | 567 | 567 | 0 |
| Delegating parent | 933 | 868 | -65 |
| Child | 601 | 554 | -47 |

These are controlled input comparisons, not provider billing measurements or a general claim that every task uses fewer tokens. Larger authorized budgets can allow more useful work and therefore more total spend. Permission/resource setup itself introduces no model call.

## Replay and historical evidence

New resource workflow histories replay in the integration checks. The broad suite also covers pre-correction v3 workflow compatibility. A separate attempt to fetch the retained September 24 live histories returned Temporal `NOT_FOUND`; no new replay of those unavailable histories is claimed. Their retained ledgers, source hashes, results and earlier replay evidence remain untouched. Historical live acceptance remains 3/4 for the September 24 campaign; this unpaid change does not establish an improved live score.

## Operational scope

No production rollout, model change, credential change or database migration was performed. The feature is enabled explicitly with `general.resources: {}`. Existing legacy configurations retain their behavior. No manual uncertain-reservation reconciliation endpoint is introduced; token admission remains an estimate with conservative handling of unknown dispatch.

## Cleanup

All isolated test schemas were removed. Seventeen orphaned workflows from this validation were terminated after their fixture schemas and workers were gone; their histories remain available under Temporal retention. The dedicated test broker was stopped, preserving its container and volume. No test worker or sandbox job container remained, and production services were not restarted.

Source hashes, full/focused test logs and the cleanup record are retained in `var/validation/resources-2026-09-26/`. Temporary scripts, logs, task-owned pytest directories and generated caches were removed after preserving that evidence. Existing source changes, unrelated files and historical paid campaigns were preserved.
