import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from scripts import model_comparison_budget as guard


def context(cell, i=0):
    return dict(scenario=cell, root_id=cell, run_id=cell + "-child", operation_id=str(i), attempt_id=str(i))


def test_concurrency_fixed_cell_caps_and_terminals(tmp_path):
    path = tmp_path / "campaign.sqlite"
    guard.initialize(path)
    for cell in guard.MANIFEST["scenario_limits"]:
        guard.admit(path, cell, cell)

    def reserve(job):
        cell, i = job
        try:
            guard.reserve(path, context(cell, i), guard.MANIFEST["scenario_caps"][cell])
            return 1
        except RuntimeError:
            return 0

    jobs = [(c, i) for c, n in guard.MANIFEST["scenario_limits"].items() for i in range(n + 4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, jobs)) == 60
    assert guard.validate(path) == guard.initialize(path) == 60
    with sqlite3.connect(path) as db:
        assert (
            dict(db.execute("SELECT scenario,count(*) FROM attempts GROUP BY scenario"))
            == guard.MANIFEST["scenario_limits"]
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM attempts")
    guard.finish(path, guard.MODELS[0] + "/bug", "failed")
    with pytest.raises(RuntimeError, match="terminal"):
        guard.admit(path, guard.MODELS[0] + "/bug", "another")
    path.unlink()
    with pytest.raises(RuntimeError, match="missing"):
        guard.initialize(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "http_failure", "unknown", "body_unknown", "refusal"])
async def test_model_root_wire_binding_and_failure_latches(tmp_path, outcome):
    path = tmp_path / "campaign.sqlite"
    guard.initialize(path)
    cell = guard.MODELS[0] + "/bug"
    guard.admit(path, cell, cell)
    sent = []

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("unknown body")

    async def send(request):
        sent.append(request)
        if outcome == "unknown":
            raise httpx.ReadError("unknown")
        if outcome == "body_unknown":
            return httpx.Response(200, stream=BrokenBody())
        return httpx.Response(
            400 if outcome == "http_failure" else 200,
            json={
                "output": [{"content": [{"type": "refusal", "refusal": "no"}]}]
                if outcome == "refusal"
                else []
            },
        )

    ctx = {**context(cell), "scenario": "bug"}
    token = request_context.set(ctx)
    body = dict(model=guard.MODELS[0], reasoning={"effort": "none"}, max_output_tokens=1024)
    try:
        async with httpx.AsyncClient(
            transport=guard.Transport(path, guard.MODELS[0], httpx.MockTransport(send))
        ) as client:
            for mutation in [
                {"model": guard.MODELS[1]},
                {"reasoning": {"effort": "low"}},
                {"max_output_tokens": 768},
            ]:
                with pytest.raises(RuntimeError):
                    await client.post(guard.MANIFEST["endpoint"], json={**body, **mutation})
            request_context.set({**ctx, "root_id": "wrong"})
            with pytest.raises(RequestNotDispatched) as refused:
                await client.post(guard.MANIFEST["endpoint"], json=body)
            assert refused.value.reason == "evaluation_limit"
            request_context.set(ctx)
            assert not sent
            if outcome in {"unknown", "body_unknown"}:
                with pytest.raises(httpx.ReadError):
                    await client.post(guard.MANIFEST["endpoint"], json=body)
            else:
                await client.post(guard.MANIFEST["endpoint"], json=body)
            if outcome == "success":
                guard.finish(path, cell, "passed")
            with pytest.raises(RuntimeError):
                await client.post(guard.MANIFEST["endpoint"], json=body)
    finally:
        request_context.reset(token)
    assert len(sent) == guard.validate(path) == 1


def test_unknown_restart_and_cross_model_admission(tmp_path):
    path = tmp_path / "campaign.sqlite"
    guard.initialize(path)
    cell = guard.MODELS[0] + "/recovery"
    guard.admit(path, cell, cell)
    guard.reserve(path, context(cell), 1024)
    assert guard.reconcile_unknown(path) == 1
    assert guard.reconcile_unknown(path) == 0
    with pytest.raises(RuntimeError, match="ambiguous"):
        guard.reserve(path, context(cell, 1), 1024)
    with pytest.raises(RuntimeError, match="Unadmitted"):
        guard.reserve(path, {**context(cell), "scenario": guard.MODELS[1] + "/recovery"}, 1024)


def test_retained_preflight_payload_parity():
    from scripts.model_comparison import DIRECTORY
    from scripts.model_comparison_audit import normalized

    path = DIRECTORY / "preflight.wire.jsonl"
    if not path.exists():
        pytest.skip("Run SDK preflight first")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for fixture in guard.LIMITS:
        initial = [
            r["body"]
            for r in records
            if r["context"]["scenario"] == fixture and r["context"]["operation_id"].endswith(":model:0")
        ]
        assert len(initial) == 3
        assert {b["model"] for b in initial} == set(guard.MODELS)
        assert all(
            b["reasoning"]["effort"] == "none" and b["max_output_tokens"] == guard.CAPS[fixture]
            for b in initial
        )
        assert len({json.dumps(normalized(b), sort_keys=True) for b in initial}) == 1
