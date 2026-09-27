import sqlite3

import pytest
from test_general_semantic import http_client, submit

from agent_runtime.general_runtime import general_action
from agent_runtime.project_store import digest
from scripts.harness_budget import MANIFEST, policy


async def test_successful_check_reused_until_workspace_changes(store):
    async with http_client(store) as client:
        workspace = await client.workspace_create({"answer.txt": b"right"})
        run = await submit(
            client,
            ["workspace_verify", "workspace_write"],
            workspace,
            {
                "outcome": "Write the exact answer",
                "criteria": [
                    {
                        "id": "bytes",
                        "statement": "Exact output",
                        "evidence_policy": "check",
                        "checks": [
                            {"id": "exact", "kind": "bytes", "path": "answer.txt", "expected": "cmlnaHQ="}
                        ],
                    }
                ],
            },
        )

        async def call(step, capability, **arguments):
            return await general_action(
                {
                    "run_id": run.id,
                    "step": step,
                    "decision": {
                        "action": {"kind": "invoke", "capability": capability, "arguments": arguments}
                    },
                }
            )

        first = await call(
            0, "workspace_verify", expected_revision=workspace["revision_id"], check_id="exact"
        )
        repeated = await call(
            1, "workspace_verify", expected_revision=workspace["revision_id"], check_id="exact"
        )
        assert repeated["reused"] and repeated["verification_ids"] == first["verification_ids"]
        changed = await call(
            2,
            "workspace_write",
            expected_revision=workspace["revision_id"],
            writes=[
                {"path": "answer.txt", "expected_sha256": digest(b"right"), "content_base64": "d3Jvbmc="}
            ],
        )
        fresh = await call(3, "workspace_verify", expected_revision=changed["revision_id"], check_id="exact")
        assert fresh["outcome"] == "fail" and not fresh.get("reused")
        assert len((await client.verifications(run.id))["items"]) == 2


def test_live_campaign_enforces_physical_cap_and_terminal_latches(tmp_path):
    guard = policy()
    ledger = tmp_path / "requests.sqlite"
    guard.initialize(ledger)
    for scenario, limit in MANIFEST["scenario_limits"].items():
        guard.admit(ledger, scenario, scenario)
        for i in range(limit):
            run_id = scenario if scenario != "parallel" or i < 4 else f"child{(i - 4) // 3}"
            guard.reserve(
                ledger,
                {
                    "scenario": scenario,
                    "run_id": run_id,
                    "root_id": scenario,
                    "operation_id": f"op:{i}",
                    "attempt_id": f"physical:{i}",
                },
                1024,
            )
        guard.finish(ledger, scenario, "complete")
        with pytest.raises(RuntimeError, match="terminal"):
            guard.admit(ledger, scenario, "different-root")
    assert guard.validate(ledger) == 16
    with sqlite3.connect(ledger) as db:
        with pytest.raises(sqlite3.IntegrityError, match="exhausted"):
            db.execute(
                "INSERT INTO attempts(scenario,run_id,root_id,operation_id,physical_attempt_id,output_cap) VALUES ('parallel','r','r','o','p',1024)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM attempts")
