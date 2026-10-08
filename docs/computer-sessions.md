# Reusable computers

Grant `computer_python` to retain an E2B computer between actions and later tasks in the same Runweave conversation (`session_id`). Guest files persist; each Python invocation starts a fresh interpreter. Only E2B is implemented. Public computer identities and lifecycle contracts belong to Runweave; provider execution and cleanup remain private adapters. This release provides bounded warm reuse. Pause/resume and desktop control are not implemented.

## Use

The capability takes the `e2b_files` artifact mappings plus a conversation-local name:

```json
{
  "computer": "analysis",
  "code": "from pathlib import Path\nPath('saved.txt').write_text('42')\nPath('answer.txt').write_text('saved')",
  "artifacts": {},
  "outputs": {"answer.txt": {"filename": "answer.txt", "media_type": "text/plain"}}
}
```

A later invocation on `analysis` can read `saved.txt`. Every invocation declares at least one output, published in that run's `RunResult.files`. Previously published files survive computer closure. Input artifact authorization and output quotas still apply. Declared output paths left by earlier operations are removed before a new command unless explicitly supplied as that command's input.

Use the media types listed in [artifact formats](artifacts.md#formats). HTML is not an installed media type; an HTML source file can be returned as `text/plain`. Job paths and output metadata are validated before any provider intent or computer admission. A command or output failure requires `needs_attention` even if the model accepts completion. Review the receipt and any published partial files; a completion assessment alone cannot reconcile a failed effect or export successful acceptance evidence.

With an already configured live-model agent granted `computer_python`:

```python
first = await client.run(
    agent.id, "Read the attached data and save working files on analysis.", files=["data.csv"]
)
second = await client.run(
    agent.id, "Continue on analysis using the saved working files.", session_id=first.session_id
)
computers = await client.computers(first.session_id)
await client.close_computer(computers[0].id)
```

Natural-language prompts require a configured live model; default fake models require scripted decisions. Close requests are asynchronous. Inspect `client.computer(id)` until `status == "closed"` to confirm completion.

## Ownership and limits

Names are scoped to a conversation. Reusing a name in another conversation creates a separate machine. Granting the capability on a later task in the same conversation grants access to the existing guest files. Use a new conversation for independent work. Child agents cannot use this capability in this release; sharing a parent's machine is never implicit. The application still has one authenticated workspace; conversation ownership does not provide tenant isolation.

The public API permits one active task per conversation. A computer also permits only one unfinished operation, including a cancelled operation awaiting cleanup. A competing operation waits without acquiring another sandbox. Closed/expired names never silently create replacements; choose a new name to start fresh. A changed operator registration cannot attach under different permissions or limits.

| Operator setting | Default | Allowed range |
| --- | --- | --- |
| `session_seconds` | 900 (15 minutes) | 60–3600 |
| `idle_seconds` | 120 (2 minutes) | 15–300 |
| `max_sessions` | 2 per conversation | 1–4 |
| `max_operations` | 32 per computer | 1–128 |

Hard expiry is fixed at creation. Successful operations refresh idle expiry, capped by hard expiry. Reconnection does not intentionally extend the hard deadline. Per-operation Python, memory, artifact and task-budget limits remain. E2B compute is billable while warm. Idle computers and unresolved cleanup retain their slot in the same four-computer pool as disposable E2B jobs. These are operator defaults, not measured capacity.

## Recovery and cleanup

Admission, ownership and creation intent are committed before provider I/O. Creation dispatch is recorded separately: a crash before dispatch may proceed, but a possibly dispatched create is recovered by metadata and never blindly repeated. Commands use distinct control directories and receipts; lost dispatch responses read the current command's receipt instead of executing again. Releasing exclusive use and saving the finished receipt are atomic. Stale error recovery cannot overwrite that finished state.

Success keeps the machine warm. An unsuccessful or cancelled in-flight operation closes its computer, retaining already published artifacts. Stopping a task after its operations finish retains the idle conversation computer until explicit close or expiry. Cleanup for an older completed operation cannot kill a computer held by a later operation. Guest code and receipts remain untrusted; they do not establish business correctness.

Explicit close and expiry prevent new use. A deterministic Temporal cleanup workflow invokes provider termination through activities; the dispatcher rediscovers unresolved or overdue cleanup after restart. A provider `NotFound` for a known handle establishes absence. Empty/ambiguous metadata searches do not. Unresolved cleanup becomes `unknown`, retaining admission and blocking reuse.

Authenticated controls:

- `GET /v1/sessions/{session_id}/computers?cursor=...&limit=100` lists metadata. Use the last returned ID as the next cursor when a page is full.
- `GET /v1/computers/{id}` shows status and expiry without provider handles or credentials.
- `POST /v1/computers/{id}/close` idempotently requests asynchronous close.
- `GET /v1/extensions/status` reports admission and computer-state counts. Healthy warm computers are excluded from pending-cleanup counts.

If independent provider/account evidence establishes termination but automatic cleanup cannot establish it, an operator may submit `POST /v1/computers/{id}/cleanup-attestation`, with `Idempotency-Key` and `{"evidence_ref": "private-evidence:reference"}`. It requires closing/unknown state and no active lease, records a distinct audit event, and releases capacity without claiming provider confirmation. Never attest merely to free capacity while termination is uncertain. Retries require the same key and evidence reference. Existing operation cleanup reconciliation remains available for unknown in-flight operations.

## Deployment and validation

Apply migration `0007`; deploy matching API/worker code and manifests. Register `ComputerCleanupWorkflow` and `cleanup_computer` on the v3 worker. Back up computer rows with the application database and keep the same provider account. Database restore cannot resurrect an expired machine. Guest files are temporary, separate from artifact backups; publish anything that must survive expiry.

```bash
uv run pytest -q tests/test_computer_sessions.py
uv run pytest -q --integration tests/test_computer_sessions_integration.py
```

Unpaid tests cover ownership, limits, shared admission, interrupted acquisition/dispatch/release, cancellation and audited recovery. PostgreSQL/Temporal tests cover continuation, worker replacement, automatic expiry and deterministic replay. Fake-model tests do not establish live-model planning quality or throughput.

For an explicitly billable execution benchmark against an already deployed, idle workspace:

```bash
uv run python -m scripts.computer_benchmark --live \
  --api-env .env.api.local --output var/benchmarks/computers-local
```

This uses real E2B compute with a scripted planner and independent host-side checks. It covers a 100,000-invoice multi-file reconciliation, incremental payment updates and replay, binary spreadsheet/image artifacts, checkpoint recovery, output/time/upload limits and admission with warm computers. `--restart-worker` additionally replaces the local Compose worker between reconciliation tasks; use it only on an idle local stack. Only benchmark-owned runs and computers are stopped during cleanup. Evidence and generated fixtures are private under the selected benchmark directory. This is a bounded execution/recovery evaluation, not a live-model or sustained-load benchmark.
