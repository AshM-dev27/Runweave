# Harness foundations

Configure optional completion review, bounded conversation memory and operator-installed extensions for the durable general task runtime. These controls complement tool grants and result contracts; see [testing and validation](validation-index.md) for their evaluation boundaries.

## Completion

A model can distinguish an unexecuted check (`pending`) from a genuinely uncertain conclusion (`inconclusive`). Pending checks run through the existing completion loop. Explicit uncertainty still blocks acceptance. Under `memory-v1`, repeated requests for a successful authoritative check reuse its receipt only when the goal, specification, branch, revision and dependency digest still match; a changed revision requires verification again.

Optional semantic review assesses answer coverage and whether source material actually supports the answer. It runs after deterministic requirements pass and cannot create evidence, waive checks, approve effects, or grant capabilities. Configure it on a general agent:

```json
{
  "name": "reviewed-research",
  "provider": "openai",
  "model": "gpt-5.6-luna",
  "max_tokens": 1024,
  "tools": ["workspace_read"],
  "general": {
    "context_policy": "memory-v1",
    "review": {
      "provider": "openai",
      "model": "gpt-5.6-luna",
      "max_tokens": 1024,
      "threshold": 0.9,
      "scope": "root"
    }
  }
}
```

Omit `review` to keep structural/evidence checks without another model judgment. An empty review object selects the agent's registered model. A different reviewer must be an installed provider/model pair. `scope: "all"` also reviews children. Every actual review request consumes the same root model/token budget, and child budgets when applicable.

Review returns `pass`, `repair`, or `defer` for every required criterion. When the task contains only optional criteria, the reviewer assesses the overall requested outcome. Missing judgments, multiple outputs, low confidence, unavailable models, and incomplete review context fail closed. The confidence threshold is a model judgment, not a calibrated probability. Review includes current authoritative check receipts and the full cited source files up to 6,000 bytes each (unrelated earlier source receipts are excluded); larger sources or a request over 14,000 bytes defer before a provider call. This deliberately bounded first implementation needs source selection before it can review large documents.

A durable review operation binds the exact answer and numeric value, goal, revision, assessments, evidence, reviewer registration and review policy. Retrying the same candidate reuses that result. Changing the candidate invalidates it. `completion.reviewed` events contain the review ID and verdict; `/v1/runs/{id}/operations` and the task assessment expose the recorded verdict. Review remains opt-in pending broader accuracy evaluation.

Parents now see merge progress derived from durable receipts and a suggested join/merge action for outstanding children. Successful merges that produce unchanged bytes still count as recorded merges. Repeated identical writes return explicit no-op feedback. This guidance does not grant capabilities or bypass final checks. New semantic assignment schemas require an explicit `outputs` list; writer capabilities require a nonempty scope before children start. Read-only work can use an empty list. This prevents a child from spending model calls before discovering that its parent omitted the file grant.

## Context and sessions

New semantic decisions include a versioned execution-progress snapshot derived from stored action receipts. It identifies the last action, consecutive reads with identical arguments, registration and results, and the model calls available after the current decision. The guidance tells the model to continue from existing observations and check its proposed answer against the task rules. Fresh reads for polling, changed state or missing data remain available; this does not cache external reads, add model calls, force completion, or change approval and acceptance checks. Budget values are a captured snapshot; server admission remains authoritative. Captured decisions retain their context on retry, and older saved bindings keep their previous instructions.

New v3 agents default to `memory-v1`; explicitly select `bounded-v3` for the previous context policy. Existing runs retain their saved policy. Memory compaction is extractive: selected earlier messages and excerpts retain source IDs, SHA-256 hashes, offsets and truncation flags. Selection combines the initial request, recent turns and keyword relevance. Historical database matches rank by the number of matching query terms before recency. Context sizing and model serialization use UTF-8 consistently. Summaries are marked untrusted.

Completed root-run inputs and outputs are the source of truth. The framework history cache keeps at most eight messages and drops older pairs to stay within its byte limit, while `session_history` retrieves exact text by message ID and offset or searches previous turns in the same session. It is automatically granted to continued root sessions with history. Other sessions and child conversations are excluded; children receive their scoped task context. Retrieval is lexical, not vector search or long-term cross-session memory.

Before a request, compaction removes older summaries, historical messages, observations, loaded skill text and excess filenames as necessary. Current input, constraints, criteria, checks and grants are preserved. If required context still exceeds the bound, the request fails rather than silently discarding obligations. Compacted source text remains retrievable through the existing read/search tools and session-history tool.

Model registrations now select `token_counter`. `utf8-v1` retains the conservative byte-based reservation and old registration identities. The configured OpenAI registrations use `o200k-v1`: tokenizer estimate plus 25% and 1,024 framing tokens, plus output reservation. Reported provider usage remains authoritative accounting. Reservations are admission estimates, not guarantees about billing; root and child limits are unchanged.

## Tools and MCP

`EXTENSION_REGISTRY_FILE` defaults to `config/extensions.json`. The manifest is trusted operator deployment configuration, loaded when each API/worker Store starts. The checked-in manifest registers the optional `browser_task` capability; see [Browser Use](browser-use.md) for its credentials and approval controls. New agent configurations select installed aliases in `tools`; discovering a tool never authorizes it. API and workers must have compatible registrations and handler code.

A read-only Streamable HTTP MCP registration looks like this (replace the endpoint, tool name and schema with the exact server contract):

```json
{
  "version": 1,
  "tools": [{
    "alias": "customer_lookup",
    "description": "Retrieve the selected customer's plan",
    "handler": "mcp.http",
    "version": 1,
    "arguments_schema": {
      "type": "object",
      "properties": {"customer": {"type": "string"}},
      "required": ["customer"],
      "additionalProperties": false
    },
    "effect": {
      "kind": "read",
      "domain": "customers",
      "approval": "none",
      "retry_safety": "read"
    },
    "config": {
      "url": "https://mcp.example.com/mcp",
      "tool": "lookup_customer",
      "credential_env": "CUSTOMER_MCP_TOKEN"
    }
  }],
  "skills": []
}
```

The worker lists tools and compares the selected tool's schema to the pinned schema before every execution. Schema drift rejects execution. The model cannot choose URLs, imports or credentials. Only JSON/text results are admitted, with bounded result size and configured credential-value redaction. Tool content is marked untrusted. HTTP MCP is supported; stdio, OAuth onboarding, MCP resources/prompts and automatic installation are not implemented.

External mutations require `kind: "external-write"`, `approval: "required"` and `retry_safety: "reconcile"`. For MCP, `config.idempotency_argument` must identify a field in the pinned upstream schema. The runtime hides that argument from the model and injects the stable operation ID. The upstream must deduplicate that ID and return its original result on retry. Declaring an argument is not proof that a server provides those semantics; the operator must verify the upstream contract before installing a write tool.

A durable intent precedes network I/O. Each custom-tool execution or reconciliation has a 45-second total timeout. New action activities renew their operation leases every two seconds, with an eight-second expiry after renewal stops; older histories and terminal operator recovery retain sixty-second leases. Ambiguous writes get one reconciliation attempt within the tool budget. Successful receipts survive an activity retry and cancellation races. Exhausted or interrupted reconciliation retains `outcome_unknown`, blocks completion and further external mutations, and leaves cleanup pending. `/effects` includes that classification. Cancellation cannot undo a remote commit. After a terminal run and lease expiry, interrupted reads and never-started calls settle without executing again; started writes remain unknown. Independent sandbox jobs are acknowledged even when another effect or child is unresolved. Final cleanup rechecks all operations and children before marking the run clean. The [operator recovery API](operator-recovery.md) can reconcile an unknown effect after the run tree stops, using the pinned handler and original operation identity. Do not resubmit the write with a new operation key.

Custom tools implement `ToolHandler.execute(ToolCall) -> dict`; external-write handlers also implement `reconcile(ToolCall) -> dict | None`. A tool definition must declare `reconciliation: "lookup"` to make that method eligible for terminal operator recovery. The default `"retry"` contract permits only the existing active-run reconciliation path; it does not authorize a write retry after cancellation. `None` means the outcome remains unknown. Register an operator-owned `module:Class` in the manifest or inject a handler instance into `ExtensionRegistry(..., handlers=...)`. Handlers are trusted host code and must not execute generated code locally. Generated code belongs in an isolated execution backend. Tool definitions, schemas and handler versions are snapshotted into each run. Keep old handler versions deployed under distinct identifiers while their runs remain resumable.

## Skills and policies

Skills are operator-installed instruction files, each limited to 4,096 UTF-8 bytes. Add a manifest entry and select its alias in `general.skills`:

```json
{"alias": "invoices", "version": 1, "description": "Invoice analysis guidance", "path": "skills/invoices.md"}
```

Paths are relative to the manifest. Content and version are pinned at submission. `skill_read` loads a selected skill on demand and later context may compact it again. Skills cannot expand tool/file grants or approve effects. This is a bounded instruction-file mechanism, without executable skill scripts, recursive includes, or marketplace installation.

`ContextPolicy` exposes `name`, `version`, `history(history, query)` and `compact(context, max_bytes)`. `ExecutionBackend` exposes `name`, `version` and async `request(identity, payload, acknowledge=False, cancel=False)`. Inject implementations through `Store(..., context_policies=ContextPolicies([...]), executors=ExecutionBackends([...]))`. Policies are selected by `general.context_policy` and `general.workspace_policy`, with their versions pinned per run; missing versions fail closed. Use distinct names to deploy versions side by side. These interfaces are trusted deployment code, not request-supplied plugins.

The bundled execution backend remains the isolated Python project broker. A custom backend must implement its immutable-revision input, bounded output-bundle, idempotent identity, cancellation and acknowledgment contract. The interface makes additional backends possible; it does not itself provide browser, computer-use, voice, arbitrary package access or general networked execution.

## Discovery and compatibility

- `GET /v1/capabilities`, `Client.installed_capabilities()`, or CLI `catalog`: installed tool descriptors without private connection configuration.
- `GET /v1/skills`, `Client.skills()`, or CLI `skills`: available skill metadata.
- Existing per-run discovery and operations APIs expose the run's selected capabilities and durable execution evidence.

Public schemas and events remain independent of the model/workflow frameworks. Persisted runs retain their snapshotted registrations and policy versions.

## Batched verification and tool receipts

New workflows expose `verify` with `check: "all"` to run pending registered checks within one durable phase. The model chooses when to verify; individual checks still use their existing scoped execution paths and consume declared tool/command capacity. Checks bind the captured revision and goal; a state change invalidates the phase. The final observation identifies the completed batch and reports each outcome, and proposing completion reuses current receipts. New semantic schemas offer only pending check choices; after verification, those choices return when the relevant state changes. Failed checks do not become accepted evidence.

Compact action history retains generic tool arguments up to a bounded size, execution status and result observations. Larger arguments are represented by a digest and key summary. Workspace actions indicate whether their bound revision remains current. These receipts help the model avoid repeating work; they do not automatically cache arbitrary tool execution or assume remote data never changes.
