# Usage

See the [README](../README.md) for startup and a scripted fake demonstration. The running API serves interactive contracts at `http://localhost:18000/docs`; source contracts are in [schemas.py](../agent_runtime/schemas.py) and [general_contracts.py](../agent_runtime/general_contracts.py).

## Python client and API

Run Python with the existing app `API_KEY` loaded (for example, `uv run --env-file .env.local python example.py`). This example uses the scripted fake provider:

```python
import asyncio
import os
from agent_runtime.client import Client
from agent_runtime.schemas import AgentConfig


async def main():
    async with Client(api_key=os.environ["API_KEY"]) as client:
        agent = await client.create_agent(AgentConfig(name="demo", provider="fake", model="deterministic"))
        run = await client.submit(agent.id, "add 2 3", idempotency_key="python-demo-1")
        print((await client.wait(run.id)).model_dump_json())


asyncio.run(main())
```

HTTP clients use `Authorization: Bearer API_KEY`. Create an agent with `POST /v1/agents`, then submit `{"agent_id":"AGENT_ID","input":"add 2 3"}` to `POST /v1/runs` with `Idempotency-Key: example-1`. Provider credentials belong in worker configuration, never requests.

Get results at `GET /v1/runs/{id}` and persisted events at `GET /v1/runs/{id}/events`. SSE reconnects using `Last-Event-ID` or `?cursor=2`; these are lifecycle/tool events, not token deltas. Disconnecting does not cancel work. Same-key/same-request retries return the original run; changed requests return 409.

CLI commands below follow `uv run --env-file .env.local python -m agent_runtime.cli`:

```text
continue RUN_ID 'previous' --key demo-2 --wait
watch RUN_ID --cursor 2
get RUN_ID
wait RUN_ID
approve RUN_ID APPROVAL_ID
deny RUN_ID APPROVAL_ID
cancel RUN_ID
```

Wait can stop for approval: review the exact arguments before deciding. Decisions are immutable. Fake `note:remember this` requests note approval; `temperature:100` exercises the read-only MCP service. Denial blocks subsequent note effects in the applicable run/tree. A new user turn creates a new run. Continuations retain completed session turns; failed/cancelled turns do not advance history. Only one active run per session is allowed.

## General runtime configuration

Use `agent --config CONFIG.json` for a complete `AgentConfig`, including `instructions`, explicit provider/model aliases and authorized `tools`. Setting `general` opts into v3; null retains legacy execution. A minimal configuration is:

```json
{"name":"general-demo","provider":"fake","model":"deterministic","tools":["add"],"general":{}}
```

Fake v3 uses scripted test actions, not general language understanding. Select an explicitly registered live provider/model for language tasks; submission may incur provider charges. `models` and `tools` list installed options, not proven availability.

Opt into shared allocation and resumable resource limits with `general.resources: {}`. Policy enforcement stays server-side; see [resource policy](resource-policy.md) for configuration, inspection, and authenticated increases.

V3 uses `general.limits` and the selected per-response `max_tokens`. Optional `general.delegation` authorizes dynamic roles with smaller grants; its tools must be a subset of parent tools. V3 rejects legacy `subagents` and `delegate`. Without `general`, toolkit agents declare up to two `subagents` and select `delegate`; `parallel_read` permits approval-free children, while `sequential` supports approval-requiring specialists. Both paths limit depth to one and lifetime children to two.

The general loop can discover authorized capabilities, invoke them, assign/join/merge work, complete or report a blocker. Discovery never grants more permissions. Caller criteria and constraints remain obligations; completion must cite admissible evidence for the current revision. Fresh integrated checks are required after edits/merges. Syntax checks prove syntax only, and generated checks are supporting evidence. Inspect receipts and outputs; model prose can be wrong.

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

`TASK.json` is a `TaskGoal` from the source contract linked above. Inspect runs with `task`, `capabilities`, `verifications`, `operations`, `checkpoints`, `children`, `budget` and `effects`. Capability/verification/operation/checkpoint listings support `--cursor` and `--limit`; capabilities also supports `--query`.

For document/CSV/repository toolkits, use `upload PATH --media-type TYPE --key KEY`, then `submit ... --artifact ID` (repeat for each input). Remembered IDs do not grant access: reattach artifacts on continuation. `download ARTIFACT_ID NEW_PATH` verifies length/SHA-256 and refuses overwrites. Repository snapshot tools produce downloadable patches without applying them.

Approve child requests at the root with the returned approval ID. Cancelling a child cancels its root tree; committed effects remain receipts. `cleanup_state` and SSE expose outstanding cleanup. See [operations](operations.md) for sandbox setup and shared resource limits, and [toolkit evidence](toolkit-validation.md) for concrete retained examples.

## Harness policies and extensions

See [harness foundations](harness-foundations.md) for optional completion review, `memory-v1` session context, installed tool/MCP aliases, selected skills, and custom context/execution policies. Discover installed capabilities with `catalog` and skill metadata with `skills`; include only authorized aliases in the agent configuration.
