"""Server-side resource admission. None of these policies are model instructions."""

import copy

RESOURCE_KEYS = ("model_attempts", "tool_attempts", "command_attempts", "total_tokens", "active_seconds")
LEGACY_CEILINGS = dict(zip(RESOURCE_KEYS, (24, 96, 16, 16000, 1800), strict=True))


class ResourceBlocked(Exception):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.detail = "resource_wait" if snapshot.get("reason") == "capacity" else "resource_limit"
        super().__init__(self.detail)


class RequestNotDispatched(RuntimeError):
    """Only a trusted adapter that has not dispatched may make this assertion."""

    def __init__(self, reason="transport_policy"):
        self.detail = "model_not_dispatched"
        self.reason = reason if reason in {"transport_policy", "evaluation_limit"} else "transport_policy"
        super().__init__(self.detail)


def not_dispatched(exc):
    # SDKs may wrap transport errors. Never infer non-dispatch from an HTTP status,
    # generic connection error, timeout or arbitrary exception text.
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, RequestNotDispatched):
            return exc
        exc = exc.__cause__
    return None


def policy(data):
    return data["policy"].get("resources")


def shared(data):
    return bool(policy(data) and policy(data)["allocation"] == "shared")


def usage(data, kind, local=False):
    budget = (
        data if local and kind == "total_tokens" else data.get("local_usage", {}) if local else data["budget"]
    )
    if kind == "total_tokens":
        return budget.get("reported_tokens", 0), budget.get("reserved_tokens", 0)
    return budget.get(kind, 0), 0


def check(root, gr, kind, required=1, reserve=0):
    """Pure check, called under the root lock before charging or performing I/O."""
    scopes = [(root.data, False, root.run_id, reserve)]
    if gr.parent_id and not shared(root.data):
        scopes.append((gr.data, True, gr.run_id, 0))
    for data, local, owner, reserved_for_parent in scopes:
        limit = data["local_limits"][kind] if local else data["policy"]["limits"][kind]
        used, held = usage(data, kind, local)
        if used + held + required > limit - reserved_for_parent:
            state = root.data.get("resource_state", {})
            raise ResourceBlocked(
                {
                    "resource": kind,
                    "scope": "child" if local else "root",
                    "owner": owner,
                    "run_id": gr.run_id,
                    "source": data.get("resource_sources", {}).get(kind, "assignment.limits")
                    if local
                    else state.get("sources", {}).get(kind, "run.limits"),
                    "limit": limit,
                    "used": used,
                    "reserved": held,
                    "required": required,
                    "finalization_reserve": reserved_for_parent,
                    "policy_version": state.get("version", 1),
                    "reason": "capacity"
                    if held and used + required <= limit - reserved_for_parent
                    else "limit",
                }
            )


def reserve_for(data, kind):
    p = policy(data)
    if not p:
        return 0
    if kind in {"tool_attempts", "command_attempts"} and not (
        data["policy"].get("delegation") or any(a.startswith("workspace_") for a in data["tools"])
    ):
        return 0
    value = p["finalization"].get(kind, 0)
    return value + (1 if kind == "model_attempts" and data["policy"].get("review") else 0)


def snapshot(root, gr):
    state = root.data["resource_state"]
    return {
        "version": state["version"],
        "allocation": policy(root.data)["allocation"],
        "on_limit": policy(root.data)["on_limit"],
        "max_pause_seconds": policy(root.data)["max_pause_seconds"],
        "finalization_reserve": {
            k: reserve_for(root.data, k) for k in RESOURCE_KEYS if k != "active_seconds"
        },
        "limits": copy.deepcopy(root.data["policy"]["limits"]),
        "ceilings": state["ceilings"],
        "sources": state["sources"],
        "usage": copy.deepcopy(root.data["budget"]),
        "child_limits": gr.data.get("local_limits") if gr.parent_id and not shared(root.data) else None,
        "child_estimate": gr.data.get("local_limits") if gr.parent_id and shared(root.data) else None,
        "pause": state.get("pause"),
    }


async def settle_model_attempt(store, run_id, attempt_id, *, tokens=None, refused=None):
    """Exactly-once settlement of a reservation, including confirmed non-dispatch."""
    from .general_db import GeneralAttemptRow

    async with store.database.sessions.begin() as db:
        _, gr, root = await store.general_lock(db, run_id, active=False)
        attempt = await db.get(GeneralAttemptRow, attempt_id)
        if attempt is None or attempt.data.get("settled"):
            return
        if tokens is None and refused is None:
            attempt.data = {**attempt.data, "outcome": "dispatch_unknown"}
            return
        amount = attempt.data["reserved_tokens"]
        data = copy.deepcopy(root.data)
        data["budget"]["reserved_tokens"] -= amount
        data["budget"]["reported_tokens"] += tokens or 0
        if refused is not None:
            data["budget"]["model_attempts"] -= 1
            if not gr.parent_id:
                data["local_usage"]["model_attempts"] -= 1
        root.data = data
        if gr.parent_id:
            child = copy.deepcopy(gr.data)
            child["reserved_tokens"] -= amount
            child["reported_tokens"] = child.get("reported_tokens", 0) + (tokens or 0)
            if refused is not None:
                child["local_usage"]["model_attempts"] -= 1
            gr.data = child
        attempt.data = {
            **attempt.data,
            "settled": True,
            "reported_tokens": tokens or 0,
            "outcome": "not_dispatched" if refused is not None else "complete",
            **({"reason": refused.reason} if refused is not None else {}),
        }
