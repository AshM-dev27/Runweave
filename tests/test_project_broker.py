"""Broker lifecycle races use a fake Docker daemon and real temporary SQLite state."""

import hashlib
import io
import json
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from sandbox import project_broker


class Docker:
    def __init__(self):
        self.lock = threading.Lock()
        self.containers = set()
        self.executions = []
        self.calls = []
        self.create_entered = threading.Event()
        self.start_entered = threading.Event()
        self.running = threading.Event()
        self.allow_create = threading.Event()
        self.allow_start = threading.Event()
        self.finish = threading.Event()
        for event in (self.allow_create, self.allow_start, self.finish):
            event.set()
        self.fail_removal = False
        self.unavailable = False
        self.start_timeout = False

    def __call__(self, *args, **kwargs):
        operation = args[0]
        with self.lock:
            self.calls.append(operation)
        if operation == "create":
            self.create_entered.set()
            assert self.allow_create.wait(5)
            name = args[args.index("--name") + 1]
            with self.lock:
                self.containers.add(name)
            return SimpleNamespace(returncode=0, stdout=b"container-id")
        if operation == "start":
            assert kwargs["input"]
            assert "--attach" in args and "--interactive" in args
            self.start_entered.set()
            assert self.allow_start.wait(5)
            with self.lock:
                if args[-1] not in self.containers:
                    return SimpleNamespace(returncode=1, stdout=b"")
                self.executions.append(args[-1])
            self.running.set()
            if self.start_timeout:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            assert self.finish.wait(5)
            # Deliberately return success after removal: cancellation must win in
            # durable state even if a successful response was already buffered.
            return SimpleNamespace(returncode=0, stdout=b'{"exit_code":0}')
        if operation == "rm":
            if self.fail_removal or self.unavailable:
                return SimpleNamespace(returncode=1, stdout=b"")
            with self.lock:
                self.containers.discard(args[-1])
            self.finish.set()
            return SimpleNamespace(returncode=0, stdout=b"")
        if operation == "ps":
            if self.unavailable:
                return SimpleNamespace(returncode=1, stdout=b"")
            name = args[args.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
            with self.lock:
                return SimpleNamespace(
                    returncode=0, stdout=b"container-id" if name in self.containers else b""
                )
        raise AssertionError(args)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    fake = Docker()
    monkeypatch.setattr(project_broker, "DB", str(tmp_path / "broker.sqlite"))
    monkeypatch.setattr(project_broker, "docker", fake)
    monkeypatch.setattr(
        project_broker, "open", lambda _: io.StringIO('{"image_digest":"pinned"}'), raising=False
    )
    project_broker.initialize()
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            yield project_broker, fake, pool
        finally:
            fake.allow_create.set()
            fake.allow_start.set()
            fake.finish.set()
    assert project_broker.LIFECYCLES == {}


def payload():
    return json.dumps({"deadline": time.time() + 90, "image_digest": "pinned"}).encode()


def stored(broker, identity="job"):
    with sqlite3.connect(broker.DB) as db:
        result, cancelled, ack = db.execute(
            "SELECT result,cancelled,ack FROM attempts WHERE id=?", (identity,)
        ).fetchone()
    return {"result": json.loads(result) if result else None, "cancelled": cancelled, "ack": ack}


def test_cancel_before_creation_prevents_late_execution(broker, monkeypatch):
    module, fake, pool = broker
    config_entered, allow_config = threading.Event(), threading.Event()

    def config(_):
        config_entered.set()
        assert allow_config.wait(5)
        return io.StringIO('{"image_digest":"pinned"}')

    monkeypatch.setattr(module, "open", config)
    body = payload()
    request = pool.submit(module.request, "job", body)
    try:
        assert config_entered.wait(5)
        assert module.cancel("job") == {"cancelled": True}
    finally:
        allow_config.set()
    expected = {"error": "sandbox_cancelled"}
    assert request.result(5) == expected
    assert module.request("job", body) == expected
    assert stored(module)["result"] == expected
    assert "create" not in fake.calls and not fake.executions


def test_cancel_before_submission_leaves_a_tombstone(broker):
    module, fake, _ = broker
    assert module.cancel("job") == {"cancelled": True}
    assert module.request("job", payload()) == {"error": "sandbox_operation_conflict"}
    assert stored(module)["result"] == {"error": "sandbox_cancelled"}
    assert "create" not in fake.calls


def test_cancel_between_creation_and_start_cannot_recreate_container(broker):
    module, fake, pool = broker
    fake.allow_start.clear()
    body = payload()
    request = pool.submit(module.request, "job", body)
    assert fake.start_entered.wait(5)
    assert module.request("job", body) == {"error": "sandbox_pending"}
    assert module.cancel("job") == {"cancelled": True}
    fake.allow_start.set()
    assert request.result(5) == {"error": "sandbox_cancelled"}
    assert module.request("job", body) == stored(module)["result"]
    assert not fake.executions and not fake.containers
    assert fake.calls.count("create") == 1


def test_cancel_waits_for_creation_without_blocking_other_jobs(broker):
    module, fake, pool = broker
    fake.allow_create.clear()
    fake.allow_start.clear()
    request = pool.submit(module.request, "job", payload())
    assert fake.create_entered.wait(5)
    cancel_entered = threading.Event()

    def cancel():
        cancel_entered.set()
        return module.cancel("job")

    cancellation = pool.submit(cancel)
    assert cancel_entered.wait(5)
    assert not cancellation.done()
    assert pool.submit(module.cancel, "other-job").result(5) == {"cancelled": True}
    fake.allow_create.set()
    assert cancellation.result(5) == {"cancelled": True}
    fake.allow_start.set()
    assert request.result(5) == {"error": "sandbox_cancelled"}
    assert not fake.executions and not fake.containers


def test_cancel_running_job_overrides_buffered_success_and_retries(broker):
    module, fake, pool = broker
    fake.finish.clear()
    body = payload()
    request = pool.submit(module.request, "job", body)
    assert fake.running.wait(5)
    assert module.cancel("job") == {"cancelled": True}
    expected = {"error": "sandbox_cancelled"}
    assert request.result(5) == expected
    assert module.request("job", body) == expected
    assert module.cancel("job") == {"cancelled": True}
    assert stored(module)["result"] == expected
    assert len(fake.executions) == 1 and not fake.containers


def test_completed_receipt_survives_cancellation_and_acknowledgement(broker):
    module, fake, _ = broker
    body = payload()
    expected = {"exit_code": 0, "image_digest": "pinned"}
    assert module.request("job", body) == expected
    assert module.cancel("job") == {"cancelled": True}
    assert module.request("job", body) == expected
    assert stored(module)["result"] == expected
    assert module.request("job", body + b" ") == {"error": "sandbox_operation_conflict"}
    assert module.acknowledge("job") == {"acknowledged": True}
    assert module.request("job", body) == {"acknowledged": True}
    assert stored(module)["ack"] == 1
    assert len(fake.executions) == 1


@pytest.mark.parametrize("failure", ["fail_removal", "unavailable"])
def test_unconfirmed_cancellation_stays_pending_until_recovery(broker, failure):
    module, fake, pool = broker
    fake.finish.clear()
    body = payload()
    request = pool.submit(module.request, "job", body)
    assert fake.running.wait(5)
    setattr(fake, failure, True)
    assert module.cancel("job") == {"error": "sandbox_pending"}
    assert stored(module) == {"result": None, "cancelled": 1, "ack": 0}
    module.acknowledge("job")
    assert stored(module)["ack"] == 0
    fake.finish.set()
    assert request.result(5) == {"error": "sandbox_pending"}
    assert module.request("job", body) == {"error": "sandbox_pending"}
    setattr(fake, failure, False)
    module.initialize()
    assert stored(module)["result"] == {"error": "sandbox_cancelled"}
    assert module.request("job", body) == {"error": "sandbox_cancelled"}
    assert len(fake.executions) == 1 and not fake.containers


def test_start_timeout_is_removed_and_replayed_without_reexecution(broker):
    module, fake, _ = broker
    fake.start_timeout = True
    body = payload()
    assert module.request("job", body) == {"error": "sandbox_timeout"}
    assert module.request("job", body) == stored(module)["result"]
    assert len(fake.executions) == 1 and not fake.containers


def test_startup_migrates_and_reconciles_legacy_pending_operations(broker):
    module, fake, _ = broker
    body = payload()
    with sqlite3.connect(module.DB) as db:
        db.execute("DROP TABLE attempts")
        db.execute(
            "CREATE TABLE attempts(id TEXT PRIMARY KEY,digest TEXT NOT NULL,"
            "result TEXT,ack INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute("INSERT INTO attempts(id,digest) VALUES (?,?)", ("job", hashlib.sha256(body).hexdigest()))
        db.execute("INSERT INTO attempts(id,digest,result) VALUES ('done','old','{\"exit_code\":0}')")
    fake.containers.add("agents-project-v3-job")
    fake.unavailable = True
    module.initialize()
    assert stored(module) == {"result": None, "cancelled": 0, "ack": 0}
    fake.unavailable = False
    module.initialize()
    assert module.request("job", body) == {"error": "sandbox_interrupted"}
    assert stored(module, "done")["result"] == {"exit_code": 0}
    assert not fake.executions and not fake.containers
