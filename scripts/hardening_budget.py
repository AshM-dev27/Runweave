"""Independent two-model hardening campaign; no historical allowance is reusable."""

import json
import sqlite3

import httpx

from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from scripts.harness_budget import policy as base_policy
from scripts.lightweight_budget import BufferedResponse

MODELS = ("gpt-4.1-mini", "gpt-5.6-luna")


def policy(model):
    if model not in MODELS:
        raise ValueError("Model outside evaluation")
    guard = base_policy()
    limits = {"resume": 3, "parallel": 12, "batch": 3, "receipt": 3, "recovery": 1}
    guard.MANIFEST = {
        **guard.MANIFEST,
        "campaign": "hardening-2026-09-27-" + model + "-v1",
        "model": model,
        "reasoning": "none" if model == "gpt-5.6-luna" else None,
        "limit": 22,
        "scenario_limits": limits,
        "scenario_caps": dict.fromkeys(limits, 1024),
        "parallel_root_limit": 8,
        "parallel_child_limit": 3,
    }
    guard.TRIGGERS = {**guard.TRIGGERS, "hard_limit": guard.TRIGGERS["hard_limit"].replace(">=16", ">=22")}
    return guard


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, ledger, guard, inner=None):
        self.ledger, self.guard = ledger, guard
        self.inner = inner or BufferedResponse(
            httpx.AsyncHTTPTransport(local_address="0.0.0.0", trust_env=False, retries=0)
        )

    async def handle_async_request(self, request):
        body = json.loads(await request.aread())
        manifest = self.guard.MANIFEST
        if (
            str(request.url) != manifest["endpoint"]
            or body.get("model") != manifest["model"]
            or body.get("reasoning", {}).get("effort") != manifest["reasoning"]
        ):
            raise RequestNotDispatched("evaluation_limit")
        context = request_context.get()
        if not context or not all(
            context.get(k) for k in ("scenario", "root_id", "run_id", "operation_id", "attempt_id")
        ):
            raise RequestNotDispatched("evaluation_limit")
        try:
            identity = self.guard.reserve(self.ledger, context, body.get("max_output_tokens"))
        except RuntimeError:
            raise RequestNotDispatched("evaluation_limit") from None
        outcome = "transport_failed_or_ambiguous"
        try:
            response = await self.inner.handle_async_request(request)
            outcome = "http_success" if response.status_code < 400 else "http_failure"
            return response
        finally:
            with sqlite3.connect(self.ledger) as db:
                db.execute("INSERT INTO outcomes VALUES (?,?)", (identity, outcome))

    async def aclose(self):
        await self.inner.aclose()


def client(ledger, guard):
    return httpx.AsyncClient(transport=Transport(ledger, guard), trust_env=False, timeout=35)
