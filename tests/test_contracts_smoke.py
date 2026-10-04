"""Unpaid transport checks for the independent six-request contracts campaign."""

import json
import sqlite3

import httpx
import pytest

from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from scripts.contracts_smoke import MANIFEST, account_terminal_campaign, policy
from scripts.hardening_budget import Transport


def context(scenario="exact"):
    return dict(
        scenario=scenario, root_id=scenario, run_id=scenario, operation_id="operation", attempt_id="attempt"
    )


def body(**updates):
    return {"model": MANIFEST["model"], "reasoning": {"effort": "none"}, "max_output_tokens": 1024, **updates}


async def test_contracts_transport_denies_wrong_request_before_dispatch(tmp_path):
    guard = policy({"source": "frozen"})
    ledger = tmp_path / "requests.sqlite"
    guard.initialize(ledger)
    guard.admit(ledger, "exact", "exact")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    token = request_context.set(context())
    try:
        async with httpx.AsyncClient(
            transport=Transport(ledger, guard, httpx.MockTransport(respond))
        ) as client:
            for payload in (
                body(model="other"),
                body(max_output_tokens=1025),
                body(reasoning={"effort": "high"}),
            ):
                with pytest.raises(RequestNotDispatched):
                    await client.post(MANIFEST["endpoint"], json=payload)
            with pytest.raises(RequestNotDispatched):
                await client.post("https://other.invalid/v1/responses", json=body())
    finally:
        request_context.reset(token)
    assert not calls and guard.validate(ledger) == 0


async def test_contracts_ambiguous_request_charged_once_and_prevents_reissue(tmp_path):
    guard = policy({"source": "frozen"})
    ledger = tmp_path / "requests.sqlite"
    guard.initialize(ledger)
    guard.admit(ledger, "exact", "exact")
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated ambiguous response")

    token = request_context.set(context())
    try:
        async with httpx.AsyncClient(
            transport=Transport(ledger, guard, httpx.MockTransport(timeout))
        ) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.post(MANIFEST["endpoint"], json=body())
            with pytest.raises(RequestNotDispatched):
                await client.post(MANIFEST["endpoint"], json=body())
    finally:
        request_context.reset(token)
    assert len(calls) == guard.validate(ledger) == 1
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT classification FROM outcomes").fetchall() == [
            ("transport_failed_or_ambiguous",)
        ]


def test_contracts_budget_manifest_and_terminals_are_immutable(tmp_path):
    guard = policy({"source": "frozen"})
    ledger = tmp_path / "requests.sqlite"
    guard.initialize(ledger)
    for name in MANIFEST["scenario_limits"]:
        guard.admit(ledger, name, name)
        for _ in range(3):
            identity = guard.reserve(ledger, context(name), 1024)
            with sqlite3.connect(ledger) as db:
                db.execute("INSERT INTO outcomes VALUES (?, 'http_success')", (identity,))
        with pytest.raises(RuntimeError, match="exhausted"):
            guard.reserve(ledger, context(name), 1024)
    assert guard.validate(ledger) == 6
    with sqlite3.connect(ledger) as db:
        with pytest.raises(sqlite3.IntegrityError, match="campaign exhausted"):
            db.execute(
                "INSERT INTO attempts(scenario,run_id,root_id,operation_id,physical_attempt_id,output_cap) VALUES ('exact','exact','exact','x','x',1024)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable campaign"):
            db.execute("DELETE FROM attempts")
    guard.finish(ledger, "exact", "passed")
    with pytest.raises(RuntimeError, match="simulated shutdown"):
        with account_terminal_campaign(guard, ledger, True):
            raise RuntimeError("simulated shutdown")
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT scenario,outcome FROM terminals ORDER BY scenario").fetchall() == [
            ("exact", "passed"),
            ("json", "interrupted"),
        ]
        manifest = json.loads(db.execute("SELECT value FROM manifest").fetchone()[0])
        assert manifest["limit"] == 6 and manifest["scenario_caps"] == {"exact": 1024, "json": 1024}
    with pytest.raises(RuntimeError, match="Scenario terminal"):
        guard.reserve(ledger, context(), 1024)
    with pytest.raises(RuntimeError, match="identity missing or changed"):
        policy({"source": "changed"}).validate(ledger)
