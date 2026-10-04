"""Fixture ground truth and simulated upstream guarantees, independent of the runtime benchmark."""

import json

import httpx
import pytest

from agent_runtime.extensions import ExtensionRegistry
from scripts.showcase_adapter import local_url, manifest
from scripts.showcase_fixtures import CRM_FIELDS, clean_customers, compare_offers, create_fixtures, csv_text
from scripts.showcase_services import app


def test_messy_crm_ground_truth_and_conservation(tmp_path):
    expected = create_fixtures(tmp_path)
    plan = clean_customers((tmp_path / "customers.csv").read_text())
    assert (
        plan["counts"]
        == expected
        == {"input_rows": 200, "accepted": 160, "duplicates": 20, "invalid": 10, "conflicts": 10}
    )
    assert len(plan["accepted"]) + len(plan["exceptions"]) == 200
    assert {row["email"] for row in plan["accepted"]} == {f"customer{i:03d}@example.test" for i in range(160)}
    assert all(row["country"] == "MY" and row["name"] == row["name"].strip() for row in plan["accepted"])


def test_conflict_quarantines_every_candidate_including_first():
    rows = [
        dict(zip(CRM_FIELDS, ["Person", "person@example.test", company, "MY"]))
        for company in ("First", "First", "Second")
    ]
    plan = clean_customers(csv_text(rows, CRM_FIELDS))
    assert plan["accepted"] == []
    assert plan["counts"]["conflicts"] == 3
    assert plan["counts"]["duplicates"] == 0


def test_pack_rounding_unknown_cost_and_variant_are_not_cheapest(tmp_path):
    create_fixtures(tmp_path)
    offers = json.loads((tmp_path / "offers.json").read_text())
    result = compare_offers(offers)
    assert result["selected"]["supplier"] == "A"
    assert result["selected"]["total_cents"] == 22500
    assert result["offers"][2]["total_cents"] is None
    assert [o["reason"] for o in result["offers"]] == [
        "qualified",
        "insufficient_stock",
        "shipping_unknown",
        "wrong_variant",
    ]
    odd = compare_offers(offers, quantity=51)
    assert odd["selected"]["packs_needed"] == 6
    assert odd["selected"]["units"] == 60
    assert odd["selected"]["total_cents"] == 26700
    assert compare_offers(offers, quantity=1000)["selected"] is None


@pytest.mark.parametrize(
    "url", ["https://api.browser-use.com", "http://example.com", "http://user@127.0.0.1"]
)
def test_simulators_cannot_target_external_services(url):
    with pytest.raises(ValueError, match="loopback"):
        local_url(url)


def test_registry_requires_approval_for_simulated_mutations():
    registry = ExtensionRegistry(manifest("http://127.0.0.1:12345"))
    for alias in ("crm_import", "refund_order", "browser_task"):
        definition = registry.tools[alias]
        assert definition["effect"]["approval"] == "required"
        assert definition["effect"]["retry_safety"] == "reconcile"
    assert registry.tools["refund_order"]["reconciliation"] == "lookup"


async def test_refund_response_loss_has_durable_idempotent_receipt(tmp_path, monkeypatch):
    create_fixtures(tmp_path)
    monkeypatch.setenv("SHOWCASE_FIXTURES", str(tmp_path))
    monkeypatch.setenv("SHOWCASE_LEDGER", str(tmp_path / "ledger.sqlite"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        request = {"order_id": "TEST-104", "amount_cents": 4900}
        headers = {"Idempotency-Key": "one-operation"}
        assert (await client.post("/refunds", json=request, headers=headers)).status_code == 503
        receipt = (await client.get("/receipts/one-operation")).json()
        assert receipt["committed"] and receipt["amount_cents"] == 4900
        assert (await client.post("/refunds", json=request, headers=headers)).json() == receipt
        assert (await client.get("/orders/TEST-104")).json()["reason"] == "already_refunded"
        state = (await client.get("/state")).json()
        assert len([key for key in state if key.startswith("refund:")]) == 1


async def test_ambiguous_receipt_stays_hidden_until_explicit_reveal(tmp_path, monkeypatch):
    create_fixtures(tmp_path)
    monkeypatch.setenv("SHOWCASE_FIXTURES", str(tmp_path))
    monkeypatch.setenv("SHOWCASE_LEDGER", str(tmp_path / "ledger.sqlite"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/refunds",
            json={"order_id": "TEST-107", "amount_cents": 2500},
            headers={"Idempotency-Key": "lost"},
        )
        assert response.status_code == 503
        assert (await client.get("/receipts/lost")).status_code == 404
        assert (await client.get("/state")).json()["refund:TEST-107"]["committed"]
        await client.post("/reveal/lost")
        assert (await client.get("/receipts/lost")).json()["amount_cents"] == 2500


async def test_browser_simulator_with_the_exact_v4_sdk_client(tmp_path, monkeypatch):
    from scripts.showcase_adapter import browser_client

    create_fixtures(tmp_path)
    monkeypatch.setenv("SHOWCASE_FIXTURES", str(tmp_path))
    monkeypatch.setenv("SHOWCASE_LEDGER", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("SHOWCASE_URL", "http://127.0.0.1:12345")
    monkeypatch.setenv("BROWSER_USE_API_KEY", "showcase-dummy-key")
    original = httpx.AsyncClient

    def http_client(*args, **kwargs):
        assert kwargs["base_url"] == "http://127.0.0.1:12345/api/v4"
        kwargs["transport"] = httpx.ASGITransport(app)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", http_client)
    async with browser_client() as client:
        run = await client.runs.create(
            "Compare the three dummy suppliers", max_cost_usd=1, browser_settings={"record": False}
        )
        assert (await client.runs.status(run.id)).status.value == "completed"
        result = await client.runs.get(run.id)
        assert json.loads(result.result)["selected"]["total_cents"] == 22500
        events = await client.runs.events(run.id)
        stopped = await client.browsers.stop(events.events[0].data["browser_session_id"])
        assert stopped.status.value == "stopped"
        abandoned = await client.runs.create("CANCEL_DEMO", max_cost_usd=1)
        assert (await client.runs.cancel(abandoned.id)).status.value == "cancelled"
        events = await client.runs.events(abandoned.id)
        assert (
            await client.browsers.stop(events.events[0].data["browser_session_id"])
        ).status.value == "stopped"
