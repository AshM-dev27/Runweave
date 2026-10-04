"""Offline acceptance export: deterministic facts, attestations, and tampering."""

import base64
import copy

import pytest
from test_general_semantic import complete, http_client, stub, submit
from test_hardening import run_checks

from agent_runtime.db import RunRow
from agent_runtime.evidence import (
    AcceptanceBundle,
    build_evidence_bundle,
    evidence_digest,
    load_evidence_bundle,
    verify_evidence_bundle,
)
from agent_runtime.general_db import GeneralRunRow
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.store import Problem


def reseal(raw):
    """A deliberate forger can recompute a seal; semantic checks must still apply."""
    raw["sha256"] = evidence_digest(raw["payload"])
    return raw


async def accepted(store, monkeypatch, *, task=None, files=None, actions=None, tools=None):
    actions = actions or [complete()]
    stub(monkeypatch, actions)
    async with http_client(store) as client:
        workspace = await client.workspace_create(files) if files is not None else None
        run = await submit(client, (tools or ["workspace_verify"]) if workspace else [], workspace, task)
        for item in actions:
            decision = await general_step({"run_id": run.id, "completion_loop": 2})
            if item["kind"] == "complete":
                result = await run_checks(run, decision)
            else:
                result = await general_action({"run_id": run.id, **decision})
        assert result["accepted"]
        return run.id


def file_task(kind="bytes"):
    return {
        "outcome": "Verify the requested output",
        "criteria": [
            {
                "id": "output",
                "statement": "Requested bytes exist",
                "evidence_policy": "check",
                "checks": [
                    {
                        "id": "output-file",
                        "kind": kind,
                        "path": "result.txt",
                        "expected": "MTIK" if kind == "bytes" else evidence_digest(b"12\n"),
                    }
                ],
            }
        ],
    }


async def test_export_direct_accepted_answer_and_strict_offline_contract(store, monkeypatch):
    run_id = await accepted(store, monkeypatch)
    bundle = await build_evidence_bundle(store, run_id)
    assert bundle == await build_evidence_bundle(store, run_id)
    assert AcceptanceBundle.model_validate_json(bundle.model_dump_json()) == bundle
    report = verify_evidence_bundle(bundle)
    assert report.valid and report.integrity_verified
    assert not report.origin_authenticated and not report.commands_executed
    assert report.judgments == ["assessment:outcome"]
    assert not report.deterministic_checks
    raw = bundle.model_dump(mode="json")
    raw["payload"]["terminal_state_version"] = True
    assert verify_evidence_bundle(reseal(raw)).errors == ["invalid_bundle_schema"]
    raw = bundle.model_dump(mode="json")
    raw["schema_version"] = 2
    assert verify_evidence_bundle(raw).errors == ["invalid_bundle_schema"]


@pytest.mark.parametrize("kind", ["bytes", "sha256"])
async def test_export_required_bytes_only_and_rerun_file_assertions(store, monkeypatch, kind):
    run_id = await accepted(
        store,
        monkeypatch,
        task=file_task(kind),
        files={"result.txt": b"12\n", "unrelated.txt": b"not required for offline validation"},
    )
    async with store.database.sessions.begin() as db:
        row = await db.get(RunRow, run_id)
        row.input = "RAW_PROMPT_DO_NOT_EXPORT"
        row.config = {**row.config, "instructions": "PRIVATE_INSTRUCTIONS_DO_NOT_EXPORT"}
        gr = await db.get(GeneralRunRow, run_id)
        gr.data = {**gr.data, "operator": {**gr.data["operator"], "credential": "SECRET_DO_NOT_EXPORT"}}
    bundle = await build_evidence_bundle(store, run_id)
    text = bundle.model_dump_json()
    assert "DO_NOT_EXPORT" not in text
    assert len(bundle.payload.blobs) == 1
    assert base64.b64decode(bundle.payload.blobs[0].content_base64) == b"12\n"
    assert len(bundle.payload.revisions[0].manifest.files) == 2
    result = verify_evidence_bundle(bundle)
    assert result.valid
    assert any(check.startswith(kind + ":") for check in result.deterministic_checks)
    # A resealed, internally linked but false expected value is independently rejected.
    raw = bundle.model_dump(mode="json")
    goal = raw["payload"]["goal"]
    spec = goal["criteria"][0]["checks"][0]
    spec["expected"] = "MTMK" if kind == "bytes" else evidence_digest(b"13\n")
    raw["payload"]["goal_sha256"] = evidence_digest(goal)
    raw["payload"]["receipts"][0]["spec_hash"] = evidence_digest(spec)
    result = verify_evidence_bundle(reseal(raw))
    assert not result.valid
    assert any(e.startswith("file_assertion_failed:") for e in result.errors)


async def test_bundle_detects_tampered_answer_content_missing_receipt_and_stale_scope(store, monkeypatch):
    run_id = await accepted(store, monkeypatch, task=file_task(), files={"result.txt": b"12\n"})
    bundle = await build_evidence_bundle(store, run_id)
    original = bundle.model_dump(mode="json")
    tampered = copy.deepcopy(original)
    tampered["payload"]["output"]["answer"] = "a changed answer"
    report = verify_evidence_bundle(tampered)
    assert "bundle_hash_mismatch" in report.errors and "output_hash_mismatch" in report.errors
    tampered = copy.deepcopy(original)
    tampered["payload"]["blobs"][0]["content_base64"] = "MTMK"
    assert any(
        e.startswith("blob_hash_or_size_mismatch:") for e in verify_evidence_bundle(reseal(tampered)).errors
    )
    tampered = copy.deepcopy(original)
    tampered["payload"]["receipts"] = []
    assert "receipt_reference_mismatch" in verify_evidence_bundle(reseal(tampered)).errors
    tampered = copy.deepcopy(original)
    tampered["payload"]["receipts"][0]["branch_id"] = "another-branch"
    assert any(
        e.startswith("receipt_binding_mismatch:") for e in verify_evidence_bundle(reseal(tampered)).errors
    )
    tampered = copy.deepcopy(original)
    tampered["payload"]["receipts"][0]["spec_hash"] = "0" * 64
    assert any(
        e.startswith("check_binding_mismatch:") for e in verify_evidence_bundle(reseal(tampered)).errors
    )


async def test_source_quote_is_verified_but_semantic_support_remains_judgment(store, monkeypatch):
    run_id = await accepted(
        store,
        monkeypatch,
        task={
            "outcome": "Cite source",
            "criteria": [{"id": "source", "statement": "Quote the source", "evidence_policy": "source"}],
        },
        files={"source.txt": "é prefix quoted words and context".encode()},
        tools=["workspace_read"],
        actions=[
            {"kind": "source", "path": "source.txt", "criterion": "c0", "quote": "quoted words"},
            complete(),
        ],
    )
    bundle = await build_evidence_bundle(store, run_id)
    report = verify_evidence_bundle(bundle)
    assert report.valid
    assert report.judgments == ["source_support:source"]
    assert any(c.startswith("source_quote:") for c in report.deterministic_checks)
    raw = bundle.model_dump(mode="json")
    raw["payload"]["receipts"][0]["source"]["offset"] += 1
    assert any(e.startswith("source_bytes_mismatch:") for e in verify_evidence_bundle(reseal(raw)).errors)


async def test_unaccepted_or_corrupted_completion_cannot_export(store, monkeypatch):
    async with http_client(store) as client:
        pending = await submit(client)
        with pytest.raises(Problem, match="evidence_requires_accepted_completion"):
            await build_evidence_bundle(store, pending.id)
        await client.cancel(pending.id)
        with pytest.raises(Problem, match="evidence_requires_accepted_completion"):
            await build_evidence_bundle(store, pending.id)
    run_id = await accepted(store, monkeypatch)
    async with store.database.sessions.begin() as db:
        row = await db.get(RunRow, run_id)
        row.output = {**row.output, "answer": "not the accepted candidate"}
    with pytest.raises(Problem, match="evidence_completion_binding_missing"):
        await build_evidence_bundle(store, run_id)


async def test_resealed_bundle_cannot_claim_authenticated_origin_or_execute_commands(store, monkeypatch):
    # Offline verification must succeed without any store access or execution API.
    run_id = await accepted(store, monkeypatch)
    bundle = await build_evidence_bundle(store, run_id)
    raw = bundle.model_dump(mode="json")
    raw["payload"]["output"]["answer"] = "An invented but structurally admissible assessment"
    raw["payload"]["output_sha256"] = evidence_digest(raw["payload"]["output"])
    report = verify_evidence_bundle(reseal(raw))
    assert report.valid  # No deterministic answer requirement was supplied.
    assert not report.origin_authenticated
    assert any("resealing" in text for text in report.limitations)


async def test_command_evidence_remains_attestation_without_running_argv(store, monkeypatch):
    run_id = await accepted(store, monkeypatch, task=file_task(), files={"result.txt": b"12\n"})
    raw = (await build_evidence_bundle(store, run_id)).model_dump(mode="json")
    payload = raw["payload"]
    spec = payload["goal"]["criteria"][0]["checks"][0]
    spec.update(
        kind="command", argv=["python", "-c", 'raise RuntimeError("never execute")'], path=None, expected=None
    )
    receipt = payload["receipts"][0]
    receipt.update(
        method="command",
        spec_hash=evidence_digest(spec),
        command={
            "argv": spec["argv"],
            "cwd": "",
            "exit_code": 0,
            "image_digest": payload["configuration"]["execution_image_digest"],
        },
    )
    payload["blobs"] = []
    payload["goal_sha256"] = evidence_digest(payload["goal"])
    report = verify_evidence_bundle(reseal(raw))
    assert report.valid and not report.commands_executed
    assert any(item.startswith("command:") for item in report.attestations)
    assert not report.deterministic_checks
    receipt["command"]["exit_code"] = 1
    assert any(
        e.startswith("command_attestation_mismatch:") for e in verify_evidence_bundle(reseal(raw)).errors
    )


async def test_untrusted_json_parser_rejects_ambiguous_nonfinite_and_deep_data(store, monkeypatch):
    run_id = await accepted(store, monkeypatch)
    bundle = await build_evidence_bundle(store, run_id)
    assert load_evidence_bundle(bundle.model_dump_json()) == bundle
    assert load_evidence_bundle(bundle.model_dump_json().encode()) == bundle
    for data in [
        '{"schema_version": 1, "schema_version": 1}',
        '{"number": NaN}',
        '{"number": 1e9999}',
        "[" * 40 + "0" + "]" * 40,
        b"\xff",
        '{"bad": "\\ud800"}',
    ]:
        with pytest.raises(ValueError):
            load_evidence_bundle(data)
    from agent_runtime.evidence import MAX_BUNDLE_BYTES

    with pytest.raises(ValueError, match="size limit"):
        load_evidence_bundle(b" " * (MAX_BUNDLE_BYTES + 1))


@pytest.mark.parametrize(
    "contract,answer",
    [
        ({"kind": "exact", "exact": "12"}, "12"),
        (
            {
                "kind": "json_schema",
                "json_schema": {
                    "type": "object",
                    "properties": {"total": {"type": "integer", "const": 12}},
                    "required": ["total"],
                    "additionalProperties": False,
                },
            },
            '{"total":12}',
        ),
    ],
)
async def test_final_answer_contract_is_independently_rechecked(store, monkeypatch, contract, answer):
    task = {
        "outcome": "Return requested result",
        "result_contract": contract,
        "criteria": [{"id": "result", "statement": "Return the result"}],
    }
    run_id = await accepted(store, monkeypatch, task=task, actions=[{**complete(), "answer": answer}])
    bundle = await build_evidence_bundle(store, run_id)
    result = verify_evidence_bundle(bundle)
    assert result.valid and "answer:" + contract["kind"] in result.deterministic_checks
    raw = bundle.model_dump(mode="json")
    raw["payload"]["output"]["answer"] = "13"
    raw["payload"]["output_sha256"] = evidence_digest(raw["payload"]["output"])
    result = verify_evidence_bundle(reseal(raw))
    assert not result.valid and any(e.startswith("result_contract:") for e in result.errors)


async def test_historical_source_is_verified_against_its_captured_revision(store, monkeypatch):
    run_id = await accepted(
        store,
        monkeypatch,
        task={
            "outcome": "Cite source",
            "criteria": [{"id": "source", "statement": "Quote the source", "evidence_policy": "source"}],
        },
        files={"source.txt": b"original quoted words"},
        tools=["workspace_read"],
        actions=[
            {"kind": "source", "path": "source.txt", "criterion": "c0", "quote": "quoted words"},
            complete(),
        ],
    )
    raw = (await build_evidence_bundle(store, run_id)).model_dump(mode="json")
    payload = raw["payload"]
    old = payload["revisions"][0]
    current = copy.deepcopy(old)
    current["manifest"]["parents"] = [old["revision_id"]]
    current["revision_id"] = evidence_digest(current["manifest"])
    payload["revisions"].append(current)
    payload["workspace"]["revision_id"] = current["revision_id"]
    payload["assessment"]["revision_id"] = current["revision_id"]
    payload["assessment_sha256"] = evidence_digest(payload["assessment"])
    result = verify_evidence_bundle(reseal(raw))
    assert result.valid
    assert any(item.startswith("Historical source revision:") for item in result.limitations)


async def test_model_review_is_exported_as_judgment(store, monkeypatch):
    from test_harness_review import reviewer

    from agent_runtime.general_completion import general_completion
    from agent_runtime.general_contracts import GeneralPolicy

    stub(monkeypatch, [complete()])
    reviewer(monkeypatch)
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(review={}))
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **decision})
        assert (await general_action(prepared))["accepted"]
    bundle = await build_evidence_bundle(store, run.id)
    result = verify_evidence_bundle(bundle)
    assert result.valid and "semantic_review" in result.judgments
    raw = bundle.model_dump(mode="json")
    raw["payload"]["assessment"]["review"]["judgments"][0]["confidence"] = 0.5
    raw["payload"]["assessment_sha256"] = evidence_digest(raw["payload"]["assessment"])
    assert "semantic_review_attestation_mismatch" in verify_evidence_bundle(reseal(raw)).errors


async def test_completed_child_bundle_keeps_scope_and_tree_identity(store, monkeypatch):
    from agent_runtime.general_completion import general_completion
    from agent_runtime.general_contracts import GeneralPolicy

    assignment = {
        "kind": "assign",
        "assignments": [
            {
                "role": "reader",
                "objective": "semantic: Read the allowed source",
                "acceptance": ["Describe the source"],
                "capabilities": ["workspace_read"],
                "inputs": ["allowed.txt"],
                "outputs": [],
            }
        ],
    }
    stub(monkeypatch, [assignment, complete()])
    async with http_client(store) as client:
        workspace = await client.workspace_create({"allowed.txt": b"allowed", "hidden.txt": b"hidden"})
        root = await submit(
            client,
            ["workspace_read"],
            workspace,
            policy=GeneralPolicy(delegation={"tools": ["workspace_read"]}),
        )
        decision = await general_step({"run_id": root.id, "completion_loop": 2})
        await general_action({"run_id": root.id, **decision})
        children = await client.children(root.id)
        child_id = children[0].id
        decision = await general_step({"run_id": child_id, "completion_loop": 2})
        result = await general_action(await general_completion({"run_id": child_id, **decision}))
        assert result["accepted"]
        bundle = await build_evidence_bundle(store, child_id)
        assert bundle.payload.parent_run_id == bundle.payload.root_run_id == root.id
        assert bundle.payload.run_id == child_id
        assert [f.path for r in bundle.payload.revisions for f in r.manifest.files] == ["allowed.txt"]
        assert not bundle.payload.blobs
        assert verify_evidence_bundle(bundle).valid


async def test_exported_schema_and_parser_reject_nested_private_fields_and_surrogates(store, monkeypatch):
    import json

    run_id = await accepted(store, monkeypatch)
    bundle = await build_evidence_bundle(store, run_id)
    raw = bundle.model_dump(mode="json")
    raw["payload"]["configuration"]["credential"] = "never accepted"
    assert verify_evidence_bundle(reseal(raw)).errors == ["invalid_bundle_schema"]
    raw = bundle.model_dump(mode="json")
    raw["payload"]["output"]["answer"] = "\ud800"
    with pytest.raises(ValueError, match="encoding"):
        load_evidence_bundle(json.dumps(raw))
    assert verify_evidence_bundle(raw).errors == ["invalid_bundle_schema"]
    schema = json.dumps(AcceptanceBundle.model_json_schema())
    assert "pydantic_ai" not in schema and "temporalio" not in schema
