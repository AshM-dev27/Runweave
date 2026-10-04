"""Authenticated export and offline verification through the public client and CLI."""

import json

import httpx
import pytest
from test_general_semantic import complete, http_client, stub, submit

from agent_runtime import cli
from agent_runtime.client import Client, ClientError
from agent_runtime.evidence import verify_evidence_bundle
from agent_runtime.general_completion import general_completion
from agent_runtime.general_runtime import general_action, general_step


async def completed_run(client, monkeypatch):
    stub(monkeypatch, [complete()])
    run = await submit(
        client,
        task={
            "outcome": "Return 12",
            "criteria": [{"id": "total", "statement": "Compute the total"}],
            "result_contract": {"kind": "exact", "exact": "12"},
        },
    )
    step = await general_step({"run_id": run.id, "completion_loop": 2})
    assert (await general_action(await general_completion({"run_id": run.id, **step})))["accepted"]
    return run


async def test_evidence_api_auth_pending_and_download_integrity(store, monkeypatch):
    async with http_client(store) as client:
        pending = await submit(client)
        with pytest.raises(ClientError, match="409"):
            await client.evidence(pending.id)
        missing = await client.http.get("/v1/runs/missing/evidence")
        assert missing.status_code == 404
        run = await completed_run(client, monkeypatch)
        bundle = await client.evidence(run.id)
        verified = verify_evidence_bundle(bundle)
        assert verified.valid and verified.integrity_verified
        assert not verified.origin_authenticated and not verified.commands_executed
        assert verified.deterministic_checks == ["answer:exact"]
        response = await client.http.get(f"/v1/runs/{run.id}/evidence")
        assert response.headers["ETag"] == '"' + bundle.sha256 + '"'
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == bundle.model_dump(mode="json")
        schema = (await client.http.get("/openapi.json")).json()
        assert "AcceptanceBundle" in schema["components"]["schemas"]
        assert "pydantic_ai" not in json.dumps(schema) and "temporalio" not in json.dumps(schema)
        client.http.headers.pop("Authorization")
        assert (await client.http.get(f"/v1/runs/{run.id}/evidence")).status_code == 401


async def test_client_rejects_corrupted_evidence_response(store, monkeypatch):
    async with http_client(store) as client:
        run = await completed_run(client, monkeypatch)
        bundle = (await client.evidence(run.id)).model_dump(mode="json")
    bundle["payload"]["output"]["answer"] = "changed"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bundle)),
        base_url="http://test",
    ) as http:
        with pytest.raises(ClientError, match="Evidence integrity"):
            await Client(http_client=http).evidence(run.id)


async def test_cli_verifies_offline_and_detects_tampering(store, monkeypatch, tmp_path, capsys):
    async with http_client(store) as client:
        run = await completed_run(client, monkeypatch)
        bundle = await client.evidence(run.id)
    path = tmp_path / "evidence.json"
    path.write_text(bundle.model_dump_json())
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(cli, "Client", lambda *a, **k: pytest.fail("Offline verification used HTTP"))
    assert await cli.run(cli.parser().parse_args(["verify-evidence", str(path)])) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    data = bundle.model_dump(mode="json")
    data["payload"]["output"]["answer"] = "wrong"
    path.write_text(json.dumps(data))
    assert await cli.run(cli.parser().parse_args(["verify-evidence", str(path)])) == 1
    assert "bundle_hash_mismatch" in json.loads(capsys.readouterr().out)["errors"]
    path.write_text('{"payload":1,"payload":2}')
    with pytest.raises(ClientError, match="Invalid or unreadable"):
        await cli.run(cli.parser().parse_args(["verify-evidence", str(path)]))


async def test_cli_export_refuses_overwrite(store, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("API_KEY", "test")
    async with http_client(store) as client:
        run = await completed_run(client, monkeypatch)
        monkeypatch.setattr(cli, "Client", lambda *a, **k: client)
        path = tmp_path / "evidence.json"
        args = cli.parser().parse_args(["evidence", run.id, "--output", str(path)])
        assert await cli.run(args) == 0
        original = path.read_bytes()
        assert json.loads(capsys.readouterr().out)["sha256"] == json.loads(original)["sha256"]
        with pytest.raises(ClientError, match="new writable path"):
            await cli.run(args)
        assert path.read_bytes() == original


async def test_historical_accepted_assessment_cannot_hide_a_recorded_denial(store, monkeypatch):
    from agent_runtime.db import RunRow

    async with http_client(store) as client:
        run = await completed_run(client, monkeypatch)
        # Historical v3 records can contain both an accepted assessment and a denied action.
        async with store.database.sessions.begin() as db:
            row = await db.get(RunRow, run.id)
            row.decisions = {"denied-action": False}
        result = await client.result(run.id)
        assert result.outcome == "blocked" and result.outcome_reason == "approval_denied"
        with pytest.raises(ClientError) as failure:
            await client.evidence(run.id)
        assert failure.value.status_code == 409
