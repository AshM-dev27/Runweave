# Operator recovery

Recovery resolves uncertain accounting and external effects on a stopped run tree. It preserves the run's terminal status and does not resume the task. API routes use the existing workspace authentication. Reconciliation performs no model call.

## Inspect and submit

Stopped deferred tools support `kind: "tool_cleanup"` with their operation ID. This calls only cleanup with saved state and cannot execute a new job. Unknown E2B acquisition remains unresolved if metadata lookup is empty/ambiguous. After independently confirming resources are gone, an operator can use `kind: "tool_cleanup_attestation"` with a required audit `evidence_ref`. The receipt identifies attestation, does not claim provider confirmation or task success, and releases held provider capacity. See [production recovery](production.md#monitoring-and-recovery).

```python
state = await client.recovery(run_id)
# state: terminal_tree, unresolved, next_cursor, reconciliations

receipt = await client.reconcile(
    run_id,
    {"kind": "external_write", "target_id": operation_id},
    idempotency_key="lookup-committed-effect-1",
)
receipt = await client.reconciliation(run_id, receipt["id"])
```

`GET /v1/runs/{id}/recovery` lists unresolved targets, with `cursor` and `limit` for pagination. `POST /v1/runs/{id}/reconciliations` accepts a request and requires `Idempotency-Key`; it returns HTTP 202 with a durable receipt. `GET /v1/runs/{id}/reconciliations/{reconciliation_id}` returns its current status. Reusing the same key and body retrieves the same operation; a changed body conflicts. Another request cannot take the same target while recovery is pending.

The CLI equivalents are `recovery RUN_ID`, `reconcile RUN_ID request.json --key KEY`, and `reconciliation RUN_ID RECONCILIATION_ID`.

A transaction stores the request and outbox record. The worker delivers a separate `ReconciliationWorkflow`, which runs I/O in an activity. Root locking, leases, operation ownership and immutable target identities prevent concurrent recovery from settling twice. `reconciliation.requested`, `reconciliation.completed` and `tool.reconciled` events expose IDs and outcome metadata, not tool arguments, receipts or credential values.

## External writes

An external-write target must already be classified `outcome_unknown`, have an expired execution lease and belong to the specified run. Both the run and its root must be terminal. The pinned tool definition must declare `reconciliation: "lookup"`. Recovery invokes only that handler's `reconcile` method, with the original operation ID and arguments. The handler must inspect the upstream idempotency receipt without creating a new effect. This is an operator-installed contract. Existing definitions default to `reconciliation: "retry"` and cannot use terminal write recovery. The bundled `mcp.http` handler reconciles by retrying a write, so it cannot declare lookup-only recovery; install a custom handler that reads the upstream receipt for that use case. Old pinned definitions without this declaration remain ineligible.

A validated handler result settles the durable effect and allows normal cleanup to proceed. A missing, invalid or unavailable result leaves it unknown. The original `execute` method is never called by this recovery path. Request status `complete` means the recovery attempt finished; inspect `result.outcome`, which is `resolved` or `unknown`.

Lookup I/O has a 45-second timeout and a 60-second lease. A crash can cause another lookup after lease expiry, so reconciliation itself must be safe to repeat. A closed recovery workflow releases its recovery lock after lease expiry while retaining any unresolved target. A new operator request may then check again. Recovery is capped at 128 submitted requests per tree to bound administrative work; it consumes no task model budget.

## Uncertain model charges

```python
receipt = await client.reconcile(
    run_id,
    {
        "kind": "model_usage",
        "target_id": attempt_id,
        "reported_tokens": 1234,
        "evidence_ref": "provider-receipt/opaque-reference",
    },
    idempotency_key="usage-receipt-1",
)
```

Use this only after obtaining actual usage from a provider receipt or equivalent evidence. The endpoint records an **operator attestation**; it does not independently query or authenticate the provider's billing record. `evidence_ref` is a bounded opaque reference, not a credential or receipt body.

Settlement replaces the held token reservation with reported usage exactly once and retains the model-attempt charge. It may record usage above the previous reservation or configured limit because the request already happened. It does not supply a missing model result, retry the request, refund an attempt, or approve task completion. Confirmed non-dispatch refunds remain reserved for trusted adapter evidence.

Existing historical reservations without an explicit `dispatch_unknown` classification are not automatically considered eligible. Live model/tool operations cannot be settled through this interface; stop the tree and wait for execution leases to expire first.
