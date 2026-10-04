"""Loopback-only CRM, payments, and Browser Use V4 simulators for the unpaid benchmark."""

import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from scripts.showcase_fixtures import clean_customers, compare_offers

app = FastAPI()
STAMP = "2026-10-04T00:00:00Z"


def connect():
    db = sqlite3.connect(os.environ["SHOWCASE_LEDGER"])
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return db


def get(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))


def add(db, key):
    put(db, key, get(db, key, 0) + 1)


def fixtures():
    return Path(os.environ["SHOWCASE_FIXTURES"])


@app.get("/health")
async def health():
    return {"ok": True, "simulation": True}


@app.get("/state")
async def state():
    with connect() as db:
        return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM state")}


@app.post("/crm/preview")
async def preview(request: Request):
    plan = clean_customers((await request.json())["csv"])
    with connect() as db:
        put(db, "plan:" + plan["plan_id"], plan)
    return {"plan_id": plan["plan_id"], "counts": plan["counts"]}


@app.post("/crm/import")
async def crm_import(request: Request):
    body = await request.json()
    key = request.headers["Idempotency-Key"]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        add(db, "crm_posts")
        old = get(db, "receipt:" + key)
        if old:
            return old
        plan = get(db, "plan:" + body["plan_id"])
        if not plan:
            raise HTTPException(404, "Plan missing")
        customers = get(db, "customers", {})
        for row in plan["accepted"]:
            if row["email"] in customers:
                raise HTTPException(409, "Customer already exists")
            customers[row["email"]] = row
        put(db, "customers", customers)
        receipt = {
            "operation_id": key,
            "plan_id": body["plan_id"],
            "imported": len(plan["accepted"]),
            "counts": plan["counts"],
            "committed": True,
        }
        put(db, "receipt:" + key, receipt)
        put(db, "crm_committed", True)
    # Response is deliberately held beyond the worker crash injection point.
    await asyncio.sleep(120)
    return receipt


@app.get("/receipts/{key:path}")
async def receipt(key: str):
    with connect() as db:
        add(db, "receipt_lookups")
        value = get(db, "receipt:" + key)
        if get(db, "hidden:" + key, False) or value is None:
            raise HTTPException(404, "Receipt unavailable")
        return value


@app.get("/orders/{order_id}")
async def order(order_id: str):
    orders = json.loads((fixtures() / "orders.json").read_text())
    if order_id not in orders:
        raise HTTPException(404)
    value = orders[order_id]
    with connect() as db:
        refunded = value["refunded"] or get(db, "refund:" + order_id) is not None
    reason = (
        "already_refunded"
        if refunded
        else "outside_refund_window"
        if value["age_days"] > 30
        else "delivered"
        if value["delivered"]
        else "eligible"
    )
    return {**value, "order_id": order_id, "eligible": reason == "eligible", "reason": reason}


@app.post("/refunds")
async def refund(request: Request):
    body = await request.json()
    key = request.headers["Idempotency-Key"]
    eligibility = await order(body["order_id"])
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        add(db, "refund_posts")
        old = get(db, "receipt:" + key)
        if old:
            return old
        if not eligibility["eligible"] or body["amount_cents"] != eligibility["amount_cents"]:
            raise HTTPException(409, "Refund ineligible or amount mismatch")
        if get(db, "refund:" + body["order_id"]):
            raise HTTPException(409, "Already refunded")
        value = {
            "operation_id": key,
            "order_id": body["order_id"],
            "amount_cents": body["amount_cents"],
            "currency": "USD",
            "refund_id": "dummy-" + str(uuid4()),
            "committed": True,
        }
        put(db, "refund:" + body["order_id"], value)
        put(db, "receipt:" + key, value)
        if body["order_id"] == "TEST-107":
            put(db, "hidden:" + key, True)
    # A proxy loses the successful response after the upstream transaction commits.
    return JSONResponse({"detail": "Injected response loss after commit"}, status_code=503)


@app.post("/reveal/{key:path}")
async def reveal(key: str):
    with connect() as db:
        put(db, "hidden:" + key, False)
    return {"revealed": True}


@app.get("/suppliers/{name}", response_class=HTMLResponse)
async def supplier(name: str):
    if name not in ("A", "B", "C"):
        raise HTTPException(404)
    return (fixtures() / f"supplier-{name}.html").read_text()


def summary(value):
    return {
        "id": value["id"],
        "status": value["status"],
        "task": value["task"],
        "title": None,
        "model": "simulated-browser",
        "contextLimit": 128000,
        "result": value["result"],
        "error": None,
        "sessionId": value["session"],
        "workspaceId": value["workspace"],
        "totalInputTokens": 0,
        "totalOutputTokens": 0,
        "totalCostUsd": "0",
        "createdAt": STAMP,
        "updatedAt": STAMP,
    }


@app.post("/api/v4/runs")
async def browser_create(request: Request):
    body = await request.json()
    assert body["maxCostUsd"] <= 1 and "model" not in body and "modelParams" not in body
    offers = []
    for name in ("A", "B", "C"):
        html = await supplier(name)
        offers += json.loads(re.search(r"<script[^>]*>(.*?)</script>", html).group(1))
    comparison = compare_offers(offers)
    comparison["sources"] = [str(request.base_url).rstrip("/") + "/suppliers/" + n for n in ("A", "B", "C")]
    identity, session, workspace, browser = [str(uuid4()) for _ in range(4)]
    value = {
        "id": identity,
        "session": session,
        "workspace": workspace,
        "browser": browser,
        "status": "running" if "CANCEL_DEMO" in body["task"] else "completed",
        "task": body["task"],
        "result": json.dumps(comparison),
    }
    with connect() as db:
        add(db, "browser_creates")
        put(db, "browser_run:" + identity, value)
        put(db, "browser:" + browser, "running")
        put(db, "browser_request:" + identity, body)
    return {
        "id": identity,
        "status": "queued",
        "model": "simulated-browser",
        "sessionId": session,
        "workspaceId": workspace,
        "eventsUrl": str(request.base_url) + "api/v4/runs/" + identity + "/events",
    }


def browser_value(identity):
    with connect() as db:
        value = get(db, "browser_run:" + identity)
    if value is None:
        raise HTTPException(404)
    return value


@app.get("/api/v4/runs/{identity}/status")
async def browser_status(identity: str):
    return {"status": browser_value(identity)["status"]}


@app.get("/api/v4/runs/{identity}")
async def browser_summary(identity: str):
    return summary(browser_value(identity))


@app.get("/api/v4/runs/{identity}/events")
async def browser_events(identity: str):
    value = browser_value(identity)
    return {
        "events": [
            {
                "runId": identity,
                "id": 1,
                "ts": STAMP,
                "type": "browser.ready",
                "data": {"browser_session_id": value["browser"]},
            }
        ],
        "nextAfter": 1,
        "hasMore": False,
    }


@app.post("/api/v4/runs/{identity}/cancel")
async def browser_cancel(identity: str):
    value = browser_value(identity)
    value["status"] = "cancelled"
    with connect() as db:
        add(db, "browser_cancels")
        put(db, "browser_run:" + identity, value)
    return summary(value)


@app.patch("/api/v4/browsers/{identity}")
async def browser_stop(identity: str, request: Request):
    assert await request.json() == {"action": "stop"}
    with connect() as db:
        add(db, "browser_stops")
        put(db, "browser:" + identity, "stopped")
    return {"id": identity, "status": "stopped", "startedAt": STAMP, "timeoutAt": STAMP}
