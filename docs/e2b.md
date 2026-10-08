# E2B remote Python

The operator-installed `e2b_files` capability runs Python on attached artifacts in a disposable E2B sandbox and publishes downloadable output artifacts. One Runweave task can process files, inspect a generated text report through `artifact_read`, and return files through `RunResult.files`. The original `e2b_python` text-job capability remains available for existing examples and saved runs.

## Artifact-based jobs

Grant `e2b_files` explicitly. Its versioned `e2b.python.v2` schema uses artifact IDs instead of inline input/output contents:

```json
{
  "code": "from pathlib import Path\nPath('answer.txt').write_text(str(Path('input.bin').stat().st_size))",
  "artifacts": {"input.bin": "ATTACHED_ARTIFACT_ID"},
  "outputs": {"answer.txt": {"filename": "answer.txt", "media_type": "text/plain"}}
}
```

Each input must be attached to this run or produced by it. Up to eight inputs totaling 64 MiB and eight outputs of up to 16 MiB each are allowed by the default registration, within the operator artifact limits and root-tree output quota. Python is limited to 20 seconds by default, configurable to at most 30 seconds; sandbox lifetime remains at most 300 seconds. Guest Python memory is limited to 512 MiB by default. These are bounded file jobs, not reusable computer sessions or desktop control.

The adapter validates file authorization and input sizes before acquiring a sandbox. Inputs are verified and streamed through SDK-signed upload endpoints; SDK 2.3.0's `files.write(IO)` buffers the entire file and is intentionally avoided for artifact bytes. Outputs are streamed into artifact storage before sandbox cleanup. Receipts contain output references with digests and sizes; binary contents and signed URLs are never persisted in handler state or workflow history.

Publication uses an operation/path identity. A lost response is recovered from committed artifact metadata; already published files are not downloaded or published again. Files survive guest expiry. Partial outputs remain visible if a later output fails, and the operation reports failure. File existence, format checks and guest exit status do not establish business correctness.

Disposable and reusable Python registrations share the same four-computer admission pool, including warm computers, unknown acquisition and pending cleanup. Existing pinned `e2b.python.v1` calls keep their original schema and limits. Deploy matching API/worker code and apply migration `0006` before admitting file jobs, and `0007` before reusable jobs. See [artifact storage](artifacts.md) for shared storage, transfer, quotas and backup requirements.

PDF, XLSX and image bytes are transferable. Parsing packages must be installed in an operator-approved template; the default template is not a guarantee that any particular parser is installed. Guest internet and credential isolation remain unchanged. For state shared across actions and later tasks, grant the separate [reusable computer](computer-sessions.md) capability `computer_python`; `e2b_files` keeps its disposable lifecycle.

The adapter uses E2B SDK **2.3.0** and the built-in `base` template by default. Custom template identifiers are operator configuration. Creating a custom template is unnecessary for the included standard-library example. References: [templates](https://docs.e2b.dev/template/quickstart), [Python SDK](https://docs.e2b.dev/sdk-reference/python-sdk/v2.3.0/sandbox_async), [metadata](https://docs.e2b.dev/sandbox/metadata).

## Setup

Use the [production profile](production.md) for deployment, migration, monitoring and operator recovery.

The default [extension manifest](../config/extensions.json) installs three Python capabilities. Grant `e2b_files` for disposable artifact jobs, `computer_python` for reusable computers, or `e2b_python` for legacy text jobs. Only the worker needs `E2B_API_KEY`; never put its value in an agent, request, manifest, event or model prompt. Keep API-only environment files free of this credential. Install the locked dependencies and deploy matching API/worker code and manifest versions before using a capability.

The local Compose worker optionally reads a private, ignored `.env.e2b.local` containing `E2B_API_KEY`. Create it with mode `0600`, then recreate the worker to apply credential changes. The API does not load this file. Local Compose requires version 2.24 or newer for optional environment files.

Disposable-job operator configuration (reusable computers add the [session settings](computer-sessions.md#ownership-and-limits)):

```json
{
  "credential_env": "E2B_API_KEY",
  "template": "base",
  "sandbox_seconds": 120,
  "command_seconds": 20
}
```

Sandbox lifetime is configurable from 30 to 300 seconds; Python execution from 1 to 30 seconds. Acquisition uses the smaller of the configured lifetime and remaining task time. Reattachment uses the remaining absolute deadline. These are duration limits, not an exact currency spending cap. E2B compute is billable.

Internet access is disabled and envd secured on creation. No provider or business credentials are injected into the guest. This bounded isolated capability has `approval: "none"`; granting it authorizes these remote compute jobs within the installed limits. It does not grant connected business-system writes or network access.

## Legacy text arguments and output

```json
{
  "code": "from pathlib import Path\nPath('answer.txt').write_text('42')",
  "files": {},
  "outputs": ["answer.txt"]
}
```

Inputs are limited to four text files totaling 16 KiB and 8 KiB of Python. Relative paths exclude traversal, hidden paths and adapter-owned control files. Up to two UTF-8 outputs of 4 KiB each are returned, with SHA-256 and byte counts. Downloads use bounded HTTP streams; signed download URLs remain in memory and are never persisted. stdout/stderr observations are each bounded to 2 KiB.

The E2B operation receipt contains the sandbox ID, performed action names, command observation, output text/digests and cleanup confirmation. Inspect it through `client.operations(run_id)`. These files are operation observations; the example saves them locally after checking their digests. Sandbox observations and exit codes remain untrusted and do not establish business correctness.

## One-task invoice demonstration

[The example](../examples/e2b_invoice.py) submits exactly one task with `client.run()`. The agent first reads an invoice CSV through Runweave, then invokes E2B. Inside the E2B operation, the adapter creates a sandbox, uploads input/program files, executes Python, downloads `cleaned.csv` and `summary.json`, and terminates the sandbox. The example inspects the persisted receipt and independently checks the expected rows and net total **130.00**.

The planner is scripted using the unpaid fake model. E2B computation, file transfer and cleanup are real. This demonstrates orchestration and provider integration; it does not evaluate autonomous model planning. One task call includes submission and waiting; HTTP progress/result retrieval still uses the existing endpoints.

For an application already running matching code with a credentialed worker:

```bash
uv run --env-file .env.api.local python -m examples.e2b_invoice
```

For an isolated demonstration using existing local PostgreSQL and Temporal:

```bash
uv run --env-file .env.e2b.local python -m scripts.e2b_demo
```

Supply `E2B_API_KEY` in the private, ignored environment file or process environment. The isolated runner starts its own API/worker processes, schema and task queue. It removes temporary logs/processes and drops its schema after success. Incomplete state is retained if dispatch or cleanup is uncertain. Generated business files go to `output/e2b-demo/` by default; `--output` selects another directory.

## Recovery and cleanup

The installed capability admits four concurrent jobs across workers using durable slots. Unknown acquisition and pending cleanup retain capacity. Waiting jobs do not create a sandbox or charge another tool attempt. Monitor authenticated `/v1/extensions/status`. Transient polling failures use bounded backoff; exhausted recovery remains explicit. Credentials are redacted before handler-state persistence. Stopped jobs support cleanup-only reconciliation and independently evidenced cleanup attestation; see [production recovery](production.md#monitoring-and-recovery).

Before creation, the operation persists an acquisition intent and opaque operation digest. E2B metadata carries that digest. If the create response disappears, the adapter looks for exactly one matching sandbox; it never repeats the create. Missing or ambiguous matches remain unknown. Metadata lookup is not an upstream uniqueness guarantee.

Before Python dispatch, the adapter persists the executing phase. A lost response causes a check for the guest receipt; the program is not launched again. If no receipt appears before the deadline, the sandbox is terminated and the job reports a timeout. Interrupted uploads may overwrite the same input bytes before execution starts.

A replacement worker waits durably while the previous worker's operation lease remains valid. Waiting performs no provider I/O and consumes no additional tool attempt; recovery continues under the original operation identity after the lease expires.

The normal result is settled after sandbox termination is confirmed. Cancellation uses the deferred cleanup path to terminate an already-acquired sandbox, without executing new code. Failed termination remains pending for cleanup retries. Unknown acquisition cannot be cleared merely because a metadata query returned no results. Provider lifetime limits provide a backstop, not proof of immediate cleanup.

## Validation

Unpaid tests cover valid output, path/input admission, lost create and command responses, missing/ambiguous lookup, cancellation, invalid output, bounded download and failed cleanup. The integration test uses a fake E2B service with real PostgreSQL/Temporal and replays the completed workflow:

```bash
uv run pytest -q tests/test_e2b.py
uv run pytest -q --integration tests/test_e2b_integration.py
```

Default tests remove `E2B_API_KEY` and cannot accidentally consume the supplied live credential. The isolated live example is an explicit opt-in and uses one sandbox for its fixture.
