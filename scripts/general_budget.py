"""Distinct immutable general-runtime-v3 transport campaign. Historical ledgers untouched."""

import json
import os
import sqlite3
import stat
from pathlib import Path

import httpx

MANIFEST = {
    "campaign": "general-runtime-v3",
    "limit": 24,
    "endpoint": "https://api.openai.com/v1/responses",
    "model": "gpt-5.6-luna",
    "reasoning": "none",
    "version": 3,
    "scenario_limits": {
        "simple": 1,
        "project-revise": 6,
        "dynamic-specialists": 10,
        "data-task": 3,
        "recovery-or-retry": 4,
    },
    "scenario_caps": {
        "simple": 512,
        "project-revise": 2048,
        "dynamic-specialists": 1024,
        "data-task": 2048,
        "recovery-or-retry": 2048,
    },
}
SCHEMA = [
    "CREATE TABLE manifest(value TEXT NOT NULL)",
    "CREATE TABLE attempts(id INTEGER PRIMARY KEY,scenario TEXT NOT NULL,run_id TEXT NOT NULL,root_id TEXT NOT NULL,operation_id TEXT NOT NULL,physical_attempt_id TEXT NOT NULL,output_cap INTEGER NOT NULL,reserved_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE outcomes(attempt_id INTEGER PRIMARY KEY,classification TEXT NOT NULL)",
]
TRIGGERS = {
    **{
        f"immutable_{table}_{action}": f"CREATE TRIGGER immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable campaign'); END"
        for table in ["manifest", "attempts", "outcomes"]
        for action in ["UPDATE", "DELETE"]
    },
    "hard_limit": "CREATE TRIGGER hard_limit BEFORE INSERT ON attempts WHEN (SELECT count(*) FROM attempts)>=24 BEGIN SELECT RAISE(ABORT,'campaign exhausted'); END",
    "immutable_manifest_insert": "CREATE TRIGGER immutable_manifest_insert BEFORE INSERT ON manifest BEGIN SELECT RAISE(ABORT,'immutable manifest'); END",
}


def checked(value):
    path = Path(value).absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise RuntimeError("Campaign symlink refused")
    if path.exists():
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077:
            raise RuntimeError("Campaign requires a private regular file")
    return path


def manifest(path):
    return json.dumps({**MANIFEST, "path": str(path)}, sort_keys=True)


def initialize(value):
    path = checked(value)
    marker = checked(path.with_suffix(".started"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return validate(path)
    if marker.exists():
        raise RuntimeError("Started campaign ledger missing")
    for target, data in [(marker, manifest(path)), (path, "")]:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    with sqlite3.connect(path) as db:
        for sql in SCHEMA:
            db.execute(sql)
        db.execute("INSERT INTO manifest VALUES (?)", (manifest(path),))
        for sql in TRIGGERS.values():
            db.execute(sql)
    return validate(path)


def validate(value):
    path = checked(value)
    marker = checked(path.with_suffix(".started"))
    if not path.exists() or not marker.exists() or marker.read_text() != manifest(path):
        raise RuntimeError("Campaign identity missing or changed")
    with sqlite3.connect(f"file:{path}?mode=rw", uri=True) as db:
        if db.execute("SELECT value FROM manifest").fetchall() != [(manifest(path),)]:
            raise RuntimeError("Campaign manifest mismatch")
        actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        if actual != TRIGGERS:
            raise RuntimeError("Campaign enforcement mismatch")
        tables = [
            row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        if sorted(tables) != sorted(SCHEMA):
            raise RuntimeError("Campaign schema mismatch")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Campaign corrupt")
        return db.execute("SELECT count(*) FROM attempts").fetchone()[0]


def reserve(value, context, cap):
    path = checked(value)
    validate(path)
    scenario = context["scenario"]
    if scenario not in MANIFEST["scenario_limits"] or cap != MANIFEST["scenario_caps"][scenario]:
        raise RuntimeError("Scenario outside campaign bounds")
    with sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            db.execute("SELECT count(*) FROM attempts WHERE scenario=?", (scenario,)).fetchone()[0]
            >= MANIFEST["scenario_limits"][scenario]
        ):
            raise RuntimeError("Scenario exhausted")
        cursor = db.execute(
            "INSERT INTO attempts(scenario,run_id,root_id,operation_id,physical_attempt_id,output_cap) VALUES (?,?,?,?,?,?)",
            (
                scenario,
                context["run_id"],
                context["root_id"],
                context["operation_id"],
                context["attempt_id"],
                cap,
            ),
        )
        db.commit()
        return cursor.lastrowid


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, path, inner=None, *, guard=None):
        import sys

        self.guard = guard or sys.modules[__name__]
        self.path = checked(path)
        self.guard.validate(self.path)
        self.inner = inner or httpx.AsyncHTTPTransport(local_address="0.0.0.0", trust_env=False, retries=0)

    async def handle_async_request(self, request):
        from agent_runtime.model_adapter import request_context

        policy = self.guard.MANIFEST
        body = json.loads(await request.aread())
        if (
            str(request.url) != policy["endpoint"]
            or body.get("model") != policy["model"]
            or body.get("reasoning", {}).get("effort") != "none"
        ):
            raise RuntimeError("Request outside campaign bounds")
        context = request_context.get()
        if not context or not all(
            context.get(k) for k in ["scenario", "run_id", "root_id", "operation_id", "attempt_id"]
        ):
            raise RuntimeError("Missing accounted context")
        try:
            identity = self.guard.reserve(self.path, context, body.get("max_output_tokens"))
        except RuntimeError:
            from agent_runtime.resources import RequestNotDispatched

            raise RequestNotDispatched("evaluation_limit") from None
        outcome = "transport_failed_or_ambiguous"
        try:
            response = await self.inner.handle_async_request(request)
            outcome = "http_success" if response.status_code < 400 else "http_failure"
            return response
        finally:
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO outcomes VALUES (?,?)", (identity, outcome))

    async def aclose(self):
        await self.inner.aclose()
