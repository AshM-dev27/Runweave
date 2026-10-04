"""Durable v3 broker protocol, separate from retained v1 job state."""

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager

LOCK = threading.Lock()
LIFECYCLES = {}
DB = "/state/projects-v3.sqlite"


def docker(*args, timeout=10, **kwargs):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout, **kwargs)


@contextmanager
def lifecycle(identity):
    """Serialize creation/removal for one job without blocking unrelated jobs."""
    with LOCK:
        lock, users = LIFECYCLES.get(identity, (threading.Lock(), 0))
        LIFECYCLES[identity] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with LOCK:
            _, users = LIFECYCLES[identity]
            if users == 1:
                del LIFECYCLES[identity]
            else:
                LIFECYCLES[identity] = (lock, users - 1)


def discard(identity):
    """An unavailable daemon must not be mistaken for a missing container."""
    name = "agents-project-v3-" + identity
    try:
        docker("rm", "-f", name)
        remaining = docker("ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}")
        return remaining.returncode == 0 and not remaining.stdout.strip()
    except Exception:
        return False


def initialize():
    with LOCK, sqlite3.connect(DB) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,digest TEXT NOT NULL,"
            "result TEXT,ack INTEGER NOT NULL DEFAULT 0,cancelled INTEGER NOT NULL DEFAULT 0)"
        )
        if "cancelled" not in {column[1] for column in db.execute("PRAGMA table_info(attempts)")}:
            db.execute("ALTER TABLE attempts ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")
        for identity, cancelled in db.execute("SELECT id,cancelled FROM attempts WHERE result IS NULL"):
            if discard(identity):
                db.execute(
                    "UPDATE attempts SET result=? WHERE id=?",
                    (
                        json.dumps({"error": "sandbox_cancelled" if cancelled else "sandbox_interrupted"}),
                        identity,
                    ),
                )


def request(identity, payload):
    fingerprint = hashlib.sha256(payload).hexdigest()
    data = json.loads(payload)
    with LOCK, sqlite3.connect(DB) as db:
        old = db.execute("SELECT digest,result,ack FROM attempts WHERE id=?", (identity,)).fetchone()
        if old:
            if old[0] != fingerprint:
                return {"error": "sandbox_operation_conflict"}
            return json.loads(old[1]) if old[1] else {"error": "sandbox_pending"}
        if not time.time() < data.get("deadline", 0) <= time.time() + 120:
            return {"error": "sandbox_expired"}
        count = db.execute("SELECT count(*) FROM attempts WHERE ack=0").fetchone()[0]
        if count >= 32:
            return {"error": "sandbox_retention_limit"}
        db.execute("INSERT INTO attempts(id,digest) VALUES (?,?)", (identity, fingerprint))
        db.commit()
    name = "agents-project-v3-" + identity
    result = {"error": "sandbox_unavailable"}
    try:
        with open("/opt/general.json") as config:
            allowed_image = json.load(config)["image_digest"]
        image = data.get("image_digest")
        if image != allowed_image:
            raise ValueError
        # Starting an existing container cannot recreate it after cancel removes it.
        # Only creation/removal need serialization; execution remains concurrent.
        with lifecycle(identity):
            with LOCK, sqlite3.connect(DB) as db:
                old = db.execute("SELECT result,cancelled FROM attempts WHERE id=?", (identity,)).fetchone()
                if old[0] is not None:
                    return json.loads(old[0])
                if old[1]:
                    return {"error": "sandbox_pending"}
            created = docker(
                "create",
                "--name",
                name,
                "--label",
                "agents.sandbox=general-v3",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "SETUID",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "KILL",
                "--cap-add",
                "DAC_OVERRIDE",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--cpus",
                "1",
                "--ulimit",
                "cpu=30:30",
                "--ulimit",
                "fsize=262144:262144",
                "--ulimit",
                "nofile=128:128",
                "--tmpfs",
                "/work:rw,noexec,nosuid,nodev,size=16m,mode=0755",
                "-i",
                image,
            )
            if created.returncode != 0:
                raise ValueError
        run = docker("start", "--attach", "--interactive", name, input=payload, timeout=65)
        if run.returncode == 0 and len(run.stdout) <= 8388608:
            result = json.loads(run.stdout)
            result["image_digest"] = image
    except subprocess.TimeoutExpired:
        result = {"error": "sandbox_timeout"}
    except Exception:
        pass
    with lifecycle(identity):
        if not discard(identity):
            return {"error": "sandbox_pending"}
        with LOCK, sqlite3.connect(DB) as db:
            old = db.execute("SELECT result,cancelled FROM attempts WHERE id=?", (identity,)).fetchone()
            if old[0] is not None:
                return json.loads(old[0])
            if old[1]:
                result = {"error": "sandbox_cancelled"}
            db.execute("UPDATE attempts SET result=? WHERE id=?", (json.dumps(result), identity))
        return result


def acknowledge(identity):
    with LOCK, sqlite3.connect(DB) as db:
        db.execute(
            "UPDATE attempts SET ack=1,result=? WHERE id=? AND result IS NOT NULL",
            ('{"acknowledged":true}', identity),
        )
    return {"acknowledged": True}


def cancel(identity):
    with lifecycle(identity):
        with LOCK, sqlite3.connect(DB) as db:
            db.execute(
                "INSERT OR IGNORE INTO attempts(id,digest,cancelled) VALUES (?,?,1)", (identity, "cancelled")
            )
            db.execute("UPDATE attempts SET cancelled=1 WHERE id=?", (identity,))
        if not discard(identity):
            return {"error": "sandbox_pending"}
        with LOCK, sqlite3.connect(DB) as db:
            db.execute(
                "UPDATE attempts SET result=? WHERE id=? AND result IS NULL",
                ('{"error":"sandbox_cancelled"}', identity),
            )
        return {"cancelled": True}
