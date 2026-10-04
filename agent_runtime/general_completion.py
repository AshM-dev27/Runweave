"""Durable completion verification, using existing action/command activities and receipts."""

import copy

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .general_db import GeneralOperationRow, GeneralRecordRow
from .project_store import digest, fail
from .resources import ResourceBlocked
from .runtime import get_store


@activity.defn
async def general_completion(data: dict):
    try:
        return await prepare_completion(data)
    except ResourceBlocked as exc:
        raise ApplicationError(exc.detail, exc.snapshot, non_retryable=True) from None


async def prepare_completion(data):
    store = get_store()
    run_id, start = data["run_id"], data["step"]
    async with store.database.sessions.begin() as db:
        row, gr, _ = await store.general_lock(db, run_id)
        model = await db.get(GeneralOperationRow, f"{run_id}:model:{start}")
        if model is None or not model.data.get("semantic_result"):
            fail("completion_binding_missing", 409)
        binding = model.data["binding"]
        batch = model.data["result"]["action"].get("arguments", {}).get("check_id") == "$pending"
        modern = binding["version"] >= 2
        from .general_receipts import current_checks

        receipts = await current_checks(store, db, gr) if modern else {}
        phase = copy.deepcopy(model.data.get("completion_phase"))
        if phase is None:
            checks = [
                dict(criterion_id=c["id"], spec=s, provenance=c["origin"])
                for c in binding["criteria"].values()
                if c["origin"] in {"user", "runtime"}
                for s in c["checks"]
                if (
                    (c["id"], s["id"]) not in receipts
                    if modern
                    else not any(
                        v["check_id"] == s["id"]
                        and v["spec_hash"] == digest(s)
                        and v["provenance"] == c["origin"]
                        for v in binding["evidence"]
                    )
                )
            ]
            phase = dict(
                version=1,
                head=binding["state"]["head"],
                goal_version=binding["goal_version"],
                state_version=binding["state"]["version"],
                start=start,
                checks=checks,
                index=0,
                evidence=[],
                status="checking",
            )
            model.data = {**model.data, "completion_phase": phase}
        if phase["status"] == "verified":
            return {"verification_done": True, "final": True}
        index = phase["index"]
        # Only a checkpoint from this exact persisted phase advances the binding.
        while index < len(phase["checks"]):
            op_id = f"{run_id}:action:{start + index}"
            op = await db.get(GeneralOperationRow, op_id)
            if not op or op.data.get("result") is None:
                break
            expected = check_decision(phase, index)
            if op.fingerprint != digest(expected):
                fail("completion_phase_conflict", 409)
            checkpoint = await db.get(GeneralRecordRow, op_id + ":checkpoint")
            if (
                checkpoint is None
                or checkpoint.data["state"]["version"] != phase["state_version"] + index + 1
            ):
                fail("completion_phase_conflict", 409)
            phase["evidence"] += op.data["result"].get("verification_ids", [])
            if op.data["result"].get("error"):
                phase["error"] = op.data["result"]["error"]
                index += 1
                break
            index += 1
        phase["index"] = index
        task = gr.data["task_state"]
        actual_head = task["head"]
        if gr.data.get("workspace"):
            branch, _ = await store.granted_branch(db, run_id)
            actual_head = branch.head
        if (actual_head, gr.data["goal"]["version"], task["version"], gr.data["step"]) != (
            phase["head"],
            phase["goal_version"],
            phase["state_version"] + index,
            start + index,
        ):
            phase["status"] = "stale"
            model.data = {**model.data, "completion_phase": phase}
            if modern:
                # Consume this obsolete proposal once and return to observation, never rebind it.
                checkpoint_id = model.id + ":stale"
                if await db.get(GeneralRecordRow, checkpoint_id + ":checkpoint") is None:
                    await store.general_checkpoint(
                        db,
                        row,
                        gr,
                        checkpoint_id,
                        {
                            "error": "stale_completion_phase",
                            "feedback": "State changed; inspect current observations and propose completion again.",
                        },
                    )
            return {"stale": True}
        if index < len(phase["checks"]) and not phase.get("error"):
            decision = check_decision(phase, index)
            final = False
        elif batch:
            receipts = await current_checks(store, db, gr)
            outcomes = {
                spec["id"]: receipts.get((c["id"], spec["id"]), {}).get("outcome", "pending")
                for c in gr.data["goal"]["criteria"]
                for spec in c["checks"]
            }
            await store.general_checkpoint(
                db,
                row,
                gr,
                model.id + ":verified",
                {
                    "action": {"kind": "verify", "check": "all"},
                    "status": "complete",
                    "checks": outcomes,
                    "outcome": "pass" if all(v == "pass" for v in outcomes.values()) else "fail",
                    "verification_ids": phase["evidence"],
                },
                progress=True,
            )
            phase["status"] = "verified"
            model.data = {**model.data, "completion_phase": phase}
            return {"verification_done": True, "final": True}
        else:
            decision = copy.deepcopy(model.data["result"])
            assessment = decision["action"]["assessment"]
            assessment["state_version"] = phase["state_version"] + index
            for vid in phase["evidence"]:
                record = await db.get(GeneralRecordRow, vid)
                if record:
                    for criterion in assessment["criteria"]:
                        if (
                            criterion["criterion_id"] == record.data["criterion_id"]
                            and vid not in criterion["evidence_ids"]
                        ):
                            criterion["evidence_ids"].append(vid)
            if modern:
                # Resolve all checks again from durable records, including checks absent from context.
                receipts = await current_checks(store, db, gr)
                goals = {c["id"]: c for c in gr.data["goal"]["criteria"]}
                for disposition in assessment["criteria"]:
                    criterion = goals[disposition["criterion_id"]]
                    if criterion["evidence_policy"] == "check":
                        evidence = [
                            receipts.get((criterion["id"], spec["id"])) for spec in criterion["checks"]
                        ]
                        disposition["evidence_ids"] = [
                            v["id"] for v in evidence if v and v["outcome"] == "pass"
                        ]
                        explicit = {
                            binding["criteria"][a["criterion"]]["id"]: a["disposition"]
                            for a in model.data.get("semantic_assessments", [])
                        }
                        if explicit.get(criterion["id"]) not in {"unsatisfied", "inconclusive"}:
                            disposition["disposition"] = (
                                "satisfied"
                                if evidence and all(v and v["outcome"] == "pass" for v in evidence)
                                else "inconclusive"
                            )
                    elif criterion["evidence_policy"] == "source":
                        from sqlalchemy import select

                        sources = await db.scalars(
                            select(GeneralRecordRow)
                            .where(GeneralRecordRow.run_id == run_id, GeneralRecordRow.kind == "verification")
                            .order_by(GeneralRecordRow.sequence)
                        )
                        disposition["evidence_ids"] = [
                            r.id
                            for r in sources
                            if r.data["criterion_id"] == criterion["id"]
                            and r.data["method"] == "source"
                            and r.data["outcome"] == "pass"
                            and r.data["goal_version"] == phase["goal_version"]
                        ][-1:]
            phase["status"] = "ready"
            final = True
        model.data = {**model.data, "completion_phase": phase}
        return {"run_id": run_id, "step": start + index, "decision": decision, "final": final}


def check_decision(phase, index):
    from .general_contracts import StepDecision

    return StepDecision.model_validate(
        {
            "action": {
                "kind": "invoke",
                "capability": "workspace_verify",
                "arguments": {
                    "expected_revision": phase["head"],
                    "check_id": phase["checks"][index]["spec"]["id"],
                },
            }
        }
    ).model_dump()
