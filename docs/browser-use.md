# Browser Use Cloud

Runweave exposes the optional `browser_task` capability through its existing task API. The worker uses `browser-use-sdk`'s V4 hosted agent; no local browser, Playwright, or browser server is installed. Existing MCP integrations remain available. The supplied `/v3/mcp` endpoint is a separate, older integration surface; this adapter uses the current V4 SDK.

## Setup and use

Set `BROWSER_USE_API_KEY` in the worker's private `.env.local`, then recreate the worker. Never put the key in agent configuration, task prompts, tool arguments, or a client application. `.env.example` contains only an empty placeholder. The API and sandbox containers do not need this credential.

The capability is registered in `config/extensions.json`. Grant `browser_task` explicitly when creating a general-runtime agent:

```python
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig

agent = await client.create_agent(
    AgentConfig(
        name="Browser researcher",
        provider="openai",  # Keep your existing registered provider/model.
        model="gpt-5.6-luna",
        tools=["browser_task"],
        general=GeneralPolicy(),
        instructions="Use browser_task for requested website research. Cite the returned sources.",
    )
)
result = await client.run(
    agent_id=agent.id,
    input="Open https://example.com and report its page title and purpose with the source URL.",
)
```

Each browser task pauses for the existing approval flow **before any paid dispatch**. Review its exact task, approve using `client.decide(run_id, approval_id, True)`, then resume with `client.result(run_id)`. Denials do not contact Browser Use. Granting the capability alone does not approve spending. Normal callers continue to use the same Runweave API.

## Controls

- Every Browser Use run sends `max_cost_usd=1.0`; operators can lower it but cannot configure more than $1. This is a provider-enforced per-browser-task cap, separate from Runweave's token budget. Multiple separately approved browser tasks have separate caps. Reported cost is retained; a reported overrun is an error, never accepted as success. This integration does not enable auto recharge or buy credits.
- Omit `model` and `model_params` by default, retaining the provider's project-compatible defaults (currently GPT-6 Luna/High for ordinary V4 projects). Operator-specified values, including an explicit empty parameter object, pass through unchanged. This does not change the outer Runweave agent's model.
- Each task gets a fresh provider session/workspace and no saved login profile, attached secrets, sharing link, or recording. The default task timeout is 300 seconds, bounded by the remaining Runweave active budget. Operators may set 10–600 seconds.
- Approval authorizes the **whole hosted browser task**. This adapter cannot approve each individual click or enforce a network/domain allowlist inside the hosted agent. It is suitable for approved public-web research and bounded browser tasks; do not treat a read-only instruction as an enforced browser permission.
- Output must come from a provider run with `completed` status, no provider error, and valid JSON matching the pinned output schema. The default schema requires a nonempty `summary` and a `sources` list. Operators can set a bounded `output_schema` in the registration. Schema validation checks structure; source accuracy and successful external actions still require independent verification. Runweave's task acceptance checks continue to apply.

## Recovery and cleanup

The worker persists an intent before creating the remote run and saves its returned identity before polling. Polls use durable Temporal timers and do not consume additional model calls or paid creates. The pinned SDK's implicit create retries are disabled.

If create times out or the worker dies before the remote ID is saved, the operation becomes `outcome_unknown`; Runweave never repeats the create or guesses identity from task text. Inspect that operation and the Browser Use dashboard before any new task. An unknown ID cannot be automatically cancelled, and the operation remains unresolved.

For known IDs, timeout and cancellation cancel unfinished runs. After terminal status, the worker drains paginated events, retains only owned browser IDs, stops those browsers, and validates stop acknowledgements before exposing a successful tool result. Live URLs, CDP URLs, raw provider events, and credentials are not persisted. A cleanup workflow makes bounded retries after worker interruption or run cancellation. If the provider stays unavailable, cleanup remains pending and the result remains unresolved; inspect the provider dashboard and restore access. No retry creates a replacement run.

Provider sessions/workspaces remain available for diagnosis; this adapter does not delete provider history or files. Stopping the browser and deleting stored history are different operations.

## Validation

```bash
uv run pytest -q tests/test_browser_use.py tests/test_harness_extensions.py
uv run pytest -q tests/test_browser_use_integration.py --integration
```

Both suites use mocked Browser Use HTTP responses with the real SDK. The integration suite additionally requires PostgreSQL and Temporal. No live spending or top-ups are performed by these tests. A live check requires separate authorization.

References: [documentation index](https://docs.browser-use.com/llms.txt), [V4 quickstart](https://docs.browser-use.com/cloud/agent/quickstart), [browser lifecycle](https://docs.browser-use.com/cloud/browser/live-preview), [run events](https://docs.browser-use.com/cloud/agent/observability).
