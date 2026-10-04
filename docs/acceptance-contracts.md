# Result contracts and acceptance evidence

Runweave can enforce a caller-defined final-answer format before accepting a general run. It can also export the accepted requirements, output and supporting receipts for offline inspection.

These controls extend the existing permission, approval, resource and evidence gates. They do not grant tools, bypass required checks, or establish that a model's reasoning is correct.

## Configure an answer contract

Add `result_contract` to the `TaskGoal` supplied as `task` when submitting a v3 run. It applies to `output.answer`, which remains a string.

For an exact answer:

```json
{
  "outcome": "Calculate five plus seven",
  "criteria": [{"id": "sum", "statement": "Calculate the total correctly"}],
  "result_contract": {"kind": "exact", "exact": "12"}
}
```

Exact matching preserves whitespace, case and Unicode representation. No trimming, extraction from prose, or normalization occurs. Exact text is limited to 4,096 UTF-8 bytes.

For a structured answer:

```json
{
  "outcome": "Calculate five plus seven and return JSON",
  "criteria": [{"id": "sum", "statement": "Calculate the total correctly"}],
  "result_contract": {
    "kind": "json_schema",
    "json_schema": {
      "type": "object",
      "properties": {"total": {"type": "integer", "minimum": 0}},
      "required": ["total"],
      "additionalProperties": false
    }
  }
}
```

The answer string must contain one JSON value with no Markdown wrapper. Duplicate keys, nonfinite numbers (including overflow), excessive nesting and values outside the schema are rejected.

Specify exact labels and units when downstream code depends on them. For example, use `"currency": {"type": "string", "enum": ["MYR"]}` and an integer `total_cents` field for an amount stored in cents, and state that convention in the task. A free-form currency string permits both `MYR` and `MYR cents`; format validation cannot infer which label the caller intended. This contract checks representation; selecting the cheapest qualifying offer still requires business-rule validation.

The supported JSON Schema subset uses Draft 2020-12 semantics for `type`, `const`, `enum`, `properties`, `required`, `additionalProperties`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `title` and `description`. Schemas are limited to 4,096 serialized UTF-8 bytes, schema nesting depth eight and 512 JSON nodes; enums have at most 64 entries. References, regular expressions, composition and unsupported keywords are rejected at submission. Validation performs no network or file access.

Contract-governed answers are limited to 16,000 UTF-8 bytes. JSON answers also have nesting depth 16 and 2,048 nodes as limits (the root has depth zero). The existing total task-context limit also applies. Use file artifacts for larger deliverables.

The saved task contract is authoritative. Model-facing context retains it during compaction; exact contracts also constrain the completion action schema. Required assessment/source dispositions must be supplied for contract-governed semantic completion. A model cannot edit the contract, and a passing format check cannot waive other task criteria, approvals, unresolved effects or required verification.

Proposals that reach acceptance validation return stable rejection gaps such as `result_contract:exact_mismatch`, `result_contract:invalid_json` or `result_contract:schema_mismatch` through the existing completion repair loop and `completion.rejected` event. Exact answers can also be rejected earlier by the constrained semantic action schema. Deterministically invalid output does not spend a semantic-review request. Repair still consumes the existing shared execution budget.

An omitted or null contract preserves previous acceptance behavior and submission fingerprints. Reusing an idempotency key with a different non-null contract returns 409. Existing run snapshots remain unchanged. Submit the desired contract for each new run; a parent's final-answer contract is not automatically imposed on child deliverables.

## Export accepted evidence

Use the authenticated endpoint `GET /v1/runs/{run_id}/evidence`, `await client.evidence(run_id)`, or:

```bash
uv run --env-file .env.local python -m agent_runtime.cli evidence RUN_ID --output acceptance.json
uv run python -m agent_runtime.cli verify-evidence acceptance.json
```

Export requires an accepted, completed v3 run with a `succeeded` task outcome and matching durable completion and verification records. A recorded approval denial prevents export even when a completion assessment says accepted. Pending, blocked, failed, cancelled or inconsistent runs cannot produce an acceptance bundle. Root and child runs export their own evidence. The client validates the bundle before returning it; CLI export refuses to overwrite an existing file.

The versioned bundle includes:

- The goal, final answer/value, completion assessment and their SHA-256 digests.
- Public policy and model/tool/skill registration identities, with execution/context versions.
- Accepted workspace and referenced source revision manifests.
- Cited authoritative check/source receipts, with only the file bytes needed to verify file assertions and source quotations.

Provider credentials, private connector configuration, raw model messages, arbitrary tool results and command logs are excluded. Task text and necessary source-file contents are included, so share bundles according to the sensitivity of those inputs. Bundles are limited to 16 MiB serialized and 4 MiB of unique decoded file content.

The response's `ETag` is the bundle's canonical payload SHA-256, not a hash of the HTTP response bytes. Responses use `Cache-Control: no-store`. Preserve the digest separately when checking a later copy against a trusted original.

## Interpret offline verification

`verify-evidence` needs no API key or running service. It never executes bundled commands, starts containers, calls a model or accesses the network. Python callers can use:

```python
from agent_runtime.evidence import load_evidence_bundle, verify_evidence_bundle

with open("acceptance.json", "rb") as source:
    bundle = load_evidence_bundle(source.read(16 * 1024 * 1024 + 1))
report = verify_evidence_bundle(bundle)
if not report.valid:
    raise ValueError(report.errors)
```

The verifier checks bounded schemas, hashes, identity/reference bindings, receipt freshness relative to the accepted revision, result contracts, exact bytes/SHA-256 file assertions and quoted source bytes. It reports these separately:

| Report field | Meaning |
| --- | --- |
| `valid` | Internal consistency and applicable deterministic assertions passed. |
| `integrity_verified` | The payload and included component hashes pass integrity checks; consult `valid` for full acceptance verification. |
| `deterministic_checks` | Assertions checked using bundled data. |
| `attestations` | Runtime claims, including command outcomes and registration identities. |
| `judgments` | Model assessments, semantic review and interpretation of sources. |
| `origin_authenticated` | Always false: this format has no signature or trusted key infrastructure. |
| `commands_executed` | Always false: command receipts are inspected, not rerun. |

An unsigned, internally consistent bundle does not prove who produced it. A quote match proves that text occurs in captured bytes, not that it supports the answer. Historical source revisions are identified explicitly; freshness does not assert that external information is still current. A passing report is not a guarantee of general task correctness.

CLI verification exits 0 on success and 1 on malformed or inconsistent evidence. Hash failures and missing or mismatched receipts remain failures even if other checks pass.
