"""Independent, bounded harness-foundations evaluation; retained campaigns are untouched."""

from functools import partial
from types import SimpleNamespace

import httpx

from scripts import general_budget as transport
from scripts import lightweight_budget as ledger

MANIFEST = {
    "campaign": "harness-foundations-2026-09-23-v1",
    "version": 1,
    "limit": 16,
    "endpoint": "https://api.openai.com/v1/responses",
    "model": "gpt-5.6-luna",
    "reasoning": "none",
    "scenario_limits": {"review_pass": 1, "review_repair": 1, "context": 4, "parallel": 10},
    "scenario_caps": {name: 1024 for name in ("review_pass", "review_repair", "context", "parallel")},
}


def policy():
    value = SimpleNamespace(
        MANIFEST=MANIFEST,
        SCHEMA=ledger.SCHEMA,
        TRIGGERS={**ledger.TRIGGERS, "hard_limit": ledger.TRIGGERS["hard_limit"].replace(">=32", ">=16")},
    )
    for name in ("manifest", "initialize", "validate", "admit", "finish", "reserve", "reconcile_unknown"):
        setattr(value, name, partial(getattr(ledger, name), policy=value))
    return value


def client(path, guard):
    inner = ledger.BufferedResponse(
        httpx.AsyncHTTPTransport(local_address="0.0.0.0", trust_env=False, retries=0)
    )
    return httpx.AsyncClient(
        transport=transport.Transport(path, inner, guard=guard), trust_env=False, timeout=35
    )
