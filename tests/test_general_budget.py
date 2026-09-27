import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_runtime.resources import RequestNotDispatched
from scripts.general_budget import MANIFEST, initialize, reserve, validate


def test_campaign_all_attempts_and_immutable_enforcement(tmp_path):
    path = tmp_path / "test.sqlite"
    initialize(path)
    jobs = [(s, i) for s, n in MANIFEST["scenario_limits"].items() for i in range(n)]

    def attempt(job):
        s, i = job
        reserve(
            path,
            {
                "scenario": s,
                "run_id": "test",
                "root_id": "test",
                "operation_id": str(i),
                "attempt_id": str(i),
            },
            MANIFEST["scenario_caps"][s],
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(attempt, jobs))
    assert validate(path) == 24
    with pytest.raises(RuntimeError):
        attempt(("simple", 25))
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO attempts(scenario,run_id,root_id,operation_id,physical_attempt_id,output_cap) VALUES ('x','x','x','x','x',512)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM attempts")
        db.execute("DROP TRIGGER hard_limit")
    with pytest.raises(RuntimeError):
        validate(path)


def test_campaign_missing_started_ledger_fails_closed(tmp_path):
    path = tmp_path / "test.sqlite"
    initialize(path)
    path.unlink()
    with pytest.raises(RuntimeError):
        initialize(path)


@pytest.mark.asyncio
async def test_transport_rejects_before_send(tmp_path):
    import httpx

    from agent_runtime.model_adapter import request_context
    from scripts.general_budget import Transport

    path = tmp_path / "guard.sqlite"
    initialize(path)
    calls = []

    async def send(request):
        calls.append(request)
        return httpx.Response(200, json={})

    transport = Transport(path, httpx.MockTransport(send))
    context = request_context.set(
        {"scenario": "simple", "run_id": "r", "root_id": "r", "operation_id": "o", "attempt_id": "a"}
    )
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            for expected in [True, False]:
                request = client.build_request(
                    "POST",
                    MANIFEST["endpoint"],
                    json={
                        "model": MANIFEST["model"],
                        "max_output_tokens": 512,
                        "reasoning": {"effort": "none"},
                    },
                )
                if expected:
                    await client.send(request)
                else:
                    with pytest.raises(RequestNotDispatched):
                        await client.send(request)
        assert len(calls) == 1 and validate(path) == 1
    finally:
        request_context.reset(context)
