# Usage

See the [README](../README.md) for startup and a scripted fake demonstration. The running API serves interactive contracts at `http://localhost:18000/docs`; source contracts are in [schemas.py](../agent_runtime/schemas.py) and [general_contracts.py](../agent_runtime/general_contracts.py).

## Give an agent a task

Configure an agent once, keep its ID, then use `client.run()` for ordinary tasks. It uploads explicitly supplied files, submits with a stable retry key, and waits for a result or a decision. It never approves actions, increases budgets or starts a replacement run automatically.

Run this example from the checkout with `uv run --env-file .env.local python example.py`. It uses the **unpaid, scripted fake provider** to demonstrate the general runtime and the client lifecycle:

```python
import asyncio
import os
from agent_runtime.client import Client
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig


async def main():
    async with Client(api_key=os.environ["API_KEY"]) as client:
        # One-time setup: save agent.id for later tasks.
        agent = await client.create_agent(
            AgentConfig(
                name="demo",
                provider="fake",
                model="deterministic",
                tools=[],
                general=GeneralPolicy(),
            )
        )
        result = await client.run(
            agent_id=agent.id,
            input="Run the scripted completion demonstration.",
        )
        print(result.answer if result.outcome == "succeeded" else result.message)
        print("Run:", result.run_id)


asyncio.run(main())
```

The fake answer is `Completed the requested scoped task.` It does not perform natural-language analysis. For real tasks, configure an installed live provider/model and the necessary authorized tools once. Provider credentials stay on the worker; model usage may incur charges. `client.models()` lists registered models. `client.installed_capabilities()` lists capabilities for the general runtime; `client.tools()` lists toolkit tools. Model selection remains yours.

With an agent configured for CSV analysis, the everyday call becomes:

```python
result = await client.run(
    agent_id=agent_id,
    input="Remove duplicate invoice IDs, include refunds, and return the net total and cleaned CSV.",
    files=["invoices.csv"],
    on_progress=lambda update: print(update.message),  # optional
)
```

`files` accepts up to eight explicitly selected local text, CSV, JSON, PDF, XLSX, PNG, JPEG, WebP, `.bin`, diff or repository ZIP files. The default per-file limit is 16 MiB; the operator can configure it. The client validates and snapshots all local files before uploads begin, then streams those same bytes on retries. Set `Client(max_file_bytes=...)` to match an operator-approved larger ceiling. Uploaded files become attached artifacts; they do not automatically become workspace files or grant tools.

General agents can use `tools=["e2b_files", "artifact_read"]` to process attached files in E2B and inspect bounded text outputs. Artifact metadata is included in model context; generated files appear in `RunResult.files`. This requires worker-side E2B credentials and the installed registrations. PDF/XLSX parsing libraries require an operator-built E2B template; allowing a file format does not install its parser. See [file storage and transfer](artifacts.md) and [E2B](e2b.md).

For small invoice CSVs with `invoice_id,amount` columns, the toolkit configuration `tools=["csv_analyze"]` and `general=None` remains available. Its existing sandbox/output limits still apply. The built-in toolkit tools belong to that runtime; general agents use workspace capabilities or installed extensions. Both use `client.run()`.

Progress callbacks can be synchronous or asynchronous. They report actual uploads and observed lifecycle changes, including the run ID immediately after submission. They do not invent tool activity or stream model tokens. Detailed SSE events remain available through `watch()`.

## Handle the outcome

`RunResult` exposes `run_id`, `session_id`, `status`, `outcome`, `outcome_reason`, `answer`, `value`, `files`, `workspace`, `message` and `next_action`. Approval details and resource information appear when relevant. `result.details` provides the full original run for optional inspection; it is excluded from the compact JSON representation. Download returned artifacts with `client.download(file.id)`; project outputs are available through `result.workspace` and workspace downloads.

`status` describes execution. `outcome` describes the task result, using recorded approvals, tool failures and acceptance checks. A `completed` run can have `outcome="blocked"`; finishing execution does not establish that the requested action succeeded. Use the outcome for application decisions:

```python
if result.outcome == "succeeded":
    print(result.answer)
else:
    print(result.message)
    print(result.next_action or "")
```

| Outcome | What to do |
| --- | --- |
| `succeeded` | Use the answer/files, subject to the configured acceptance requirements. This is not a general guarantee of factual correctness. |
| `blocked` | An action was denied, a file was outside scope, or the general agent stopped blocked. Review the reason and any committed effects before starting a new task. |
| `needs_attention` | For `awaiting_approval`, explicitly approve or deny the listed action. For `paused_budget`, inspect resources and increase an allowed budget or cancel. For `completed`, review unresolved tool/specialist errors or an unavailable historical outcome. |
| `failed` | Inspect the error and effect receipts before another submission. Earlier effects may have committed. |
| `cancelled` | Execution stopped; the convenience result waits for cleanup. |
| `in_progress` | The HTTP run is queued/running, or recorded approval decisions are being processed. `run()`/`result()` continue waiting. |

A recorded approval denial keeps the final task outcome `blocked` even if a later v3 completion assessment says accepted. The execution status may still be `completed`; inspect `outcome` before treating the task as successful. Such a run cannot export an acceptance bundle.

A successful retry of the same durable tool operation clears its failure. Other unresolved tool failures remain visible for review, including general-runtime command or output failures when the model accepts completion. Such a task requires `needs_attention` and cannot export successful acceptance evidence. Older file-tool runs without outcome tracking return `needs_attention` with `outcome_unavailable`; older recorded approval denials can still be identified. Answer wording never overrides server outcome facts. New lifecycle SSE events include `outcome` and `outcome_reason`; historical events remain unchanged. Clients can fetch the current run projection when replaying older events.

After the user has reviewed and approved a specific action:

```python
# approval_id is the particular approval the user reviewed and chose to approve.
await client.approve(result.run_id, approval_id)
result = await client.result(result.run_id)
```

Approvals may include a `preview` with a readable title, bounded facts and warnings. The task client and CLI display these facts; the exact `tool` and `arguments` remain available for review. Operators define the presentation. Receipt-derived facts appear only when the previous stored tool result matches the proposed arguments; missing or mismatched evidence produces a warning. Treat the preview as a summary of the authorized action, not independent proof that model reasoning is correct.

Use `deny()` for a denial. `result()` resumes waiting on the same task without resubmitting, deciding approvals, or increasing limits. It can return another approval or resource pause. Approval and budget waits are normal outcomes, not automatic errors. The public approval list contains only undecided actions. After all decisions are recorded, the client waits through the worker handoff instead of requesting the same approval again. See [resource decisions](resource-policy.md#inspect-and-resume) when a limit requires attention.

## Interrupted requests and recovery

`timeout` controls how long the client waits **after submission**; it does not cancel server execution. Uploads and submission have their own HTTP timeouts. A local timeout or progress callback failure raises `ClientError` with the known `run_id` and original `idempotency_key`. Resume with `client.result(error.run_id)` when an ID is available.

For an ambiguous upload or submission without a run ID, repeat `client.run()` with the **same input, file contents/order and `error.idempotency_key`**. Upload slots and submission identities remain stable, so retries reuse committed work. Changed input with a retained key returns a conflict. A new key means a new task and may repeat effects. Supply your own key before calling if you need recovery across application restarts or process interruption.

Only transport retries are automatic; HTTP errors, task failures and approval decisions are never blindly retried. `ClientError.code`, `status_code` and `fields` provide safe diagnostics. Validation errors name known fields without echoing input values, server exception text or credentials. A callback exception is reported safely and does not cancel a submitted task.

## Python client and API

For an existing agent, the CLI supports the same simple flow:

```bash
uv run --env-file .env.local python -m agent_runtime.cli run AGENT_ID "Clean duplicate invoices and calculate the net total" --file invoices.csv
uv run --env-file .env.local python -m agent_runtime.cli result RUN_ID
```

Progress and the retry key go to stderr. The answer and run ID go to stdout; `--json` prints the compact outcome for scripts. Task command exit codes are 0 for `succeeded`, 2 for `blocked` or `needs_attention`, and 1 for failure/cancellation. `--timeout` stops waiting without cancelling. Existing `submit`, `wait`, `watch`, `approve`, `deny`, `cancel` and inspection commands remain available.

For direct HTTP, use **Authorize** in `/docs` with the application `API_KEY`. Configure an agent with `POST /v1/agents`, then send `{"agent_id":"AGENT_ID","input":"Your task"}` to `POST /v1/runs` with an `Idempotency-Key` header. `GET /v1/runs/{id}` retrieves the status/result; `/events` streams lifecycle/tool events and resumes with `Last-Event-ID`. The Python convenience method coordinates these existing endpoints; it does not add a separate execution API.

A new turn uses a new submission key and the prior `session_id`. Only one active run per session is allowed. The lower-level `continue_run()` helper retains the session; explicitly reattach files/workspaces and task requirements for each new turn. Failed or cancelled turns do not advance conversation history.

## General runtime configuration

Use `agent --config CONFIG.json` for a complete `AgentConfig`, including `instructions`, explicit provider/model aliases and authorized `tools`. Setting `general` opts into v3; null retains legacy execution. A minimal configuration is:

```json
{"name":"general-demo","provider":"fake","model":"deterministic","tools":["add"],"general":{}}
```

Fake v3 uses scripted test actions, not general language understanding. Select an explicitly registered live provider/model for language tasks; submission may incur provider charges. Use CLI `catalog` or `client.installed_capabilities()` for general-runtime tool aliases, and CLI `tools` or `client.tools()` for toolkit aliases. `models` lists registered provider/model pairs. Discovery does not prove that a provider or external service is available.

New agents automatically size tasks from incoming input and attachment metadata, then grow output/context capacity from observed demand within pinned limits. No size profile is required. Explicit `max_tokens` remains a hard cap; omit it to use automatic output sizing. The selected model, tool grants and approval requirements remain unchanged. See [automatic task sizing](resource-policy.md#automatic-task-sizing).

New general policies default to shared allocation and resumable resource limits. Omitted compute budgets impose no task-specific cap; the pinned operator and model ceilings remain visible through `/resources`. Set `general.resources: null` only to retain legacy defaults. Policy enforcement stays server-side; see [resource policy](resource-policy.md) for configuration, inspection, and authenticated increases.

V3 uses `general.limits` and the selected per-response `max_tokens`. Optional `general.delegation` authorizes dynamic roles with smaller grants; its tools must be a subset of parent tools. V3 rejects legacy `subagents` and `delegate`. Without `general`, toolkit agents declare up to two `subagents` and select `delegate`; `parallel_read` permits approval-free children, while `sequential` supports approval-requiring specialists. Both paths limit depth to one and lifetime children to two.

The general loop can discover authorized capabilities, invoke them, assign/join/merge work, complete or report a blocker. Discovery never grants more permissions. Caller criteria and constraints remain obligations; completion must cite admissible evidence for the current revision. Fresh integrated checks are required after edits/merges. Syntax checks prove syntax only, and generated checks are supporting evidence. Inspect receipts and outputs; model prose can be wrong.

## Result contracts and acceptance evidence

Supply `task.result_contract` to enforce exact final-answer text or a bounded JSON Schema. A mismatch returns through the existing repair loop and cannot be waived by the model. Existing submissions without this field retain their behavior.

For accepted v3 runs, `Client.evidence(run_id)` or `evidence RUN_ID --output FILE` exports a validated acceptance bundle. `verify-evidence FILE` checks it offline without an API key or executing bundled commands. See [result contracts and evidence](acceptance-contracts.md) for examples, bounds and the distinction between checked assertions, runtime attestations and model judgments.

## Workspaces, artifacts and inspection

Project commands reconstruct disposable isolated containers from immutable workspace revisions. Attach the workspace/revision explicitly on every submission or continuation. Writes and merges require expected heads/hashes; conflicts require resolution. Child branches contain only granted files and writable prefixes.

```text
workspace-create --directory DIR --key workspace-1
workspace ID --revision REVISION
submit AGENT_ID INPUT --task TASK.json --workspace ID --revision REVISION --key task-1 --wait
workspace-read ID --revision REVISION --file RELATIVE_PATH NEW_LOCAL_PATH
workspace-download ID --revision REVISION NEW_LOCAL_ZIP
operation-output RUN_ID OPERATION_ID OUTPUT_NAME NEW_LOCAL_PATH
```

`TASK.json` is a `TaskGoal` from the source contract linked above. Inspect runs with `task`, `capabilities`, `verifications`, `operations`, `checkpoints`, `children`, `budget`, `resources`, `recovery` and `effects`. See [operator recovery](operator-recovery.md) for durable reconciliation requests. Capability/verification/operation/checkpoint listings support `--cursor` and `--limit`; capabilities also supports `--query`.

For document/CSV/repository toolkits, use `upload PATH --media-type TYPE --key KEY`, then `submit ... --artifact ID` (repeat for each input). Remembered IDs do not grant access: reattach artifacts on continuation. `download ARTIFACT_ID NEW_PATH` verifies length/SHA-256 and refuses overwrites. Repository snapshot tools produce downloadable patches without applying them.

Approve child requests at the root with the returned approval ID. Cancelling a child cancels its root tree; committed effects remain receipts. `cleanup_state` and SSE expose outstanding cleanup. See [operations](operations.md) for sandbox setup and shared resource limits, and the [business scenarios](validation-index.md#reproducible-business-scenarios) for a reproducible recovery demonstration.

## Harness policies and extensions

See [harness foundations](harness-foundations.md) for optional completion review, `memory-v1` session context, installed tool/MCP aliases, selected skills, and custom context/execution policies. Discover installed capabilities with `catalog` and skill metadata with `skills`; include only authorized aliases in the agent configuration.
