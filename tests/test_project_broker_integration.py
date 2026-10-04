"""Real Docker lifecycle checks; no application services or model calls are needed."""

import base64
import io
import json
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from sandbox import project_broker

pytestmark = pytest.mark.integration


def docker(*args, timeout=10, **kwargs):
    return subprocess.run(["sudo", "-n", "docker", *args], capture_output=True, timeout=timeout, **kwargs)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    configuration = (Path(__file__).resolve().parents[1] / "config/general.json").read_text()
    policy = json.loads(configuration)
    image = docker("image", "inspect", policy["image_digest"])
    assert image.returncode == 0, "The configured project sandbox image must be available"
    names = set()
    start_entered, allow_start = threading.Event(), threading.Event()
    allow_start.set()

    def command(*args, **kwargs):
        if args[0] == "create":
            names.add(args[args.index("--name") + 1])
        if args[0] == "start":
            start_entered.set()
            assert allow_start.wait(10)
        return docker(*args, **kwargs)

    monkeypatch.setattr(project_broker, "DB", str(tmp_path / "broker.sqlite"))
    monkeypatch.setattr(project_broker, "docker", command)
    monkeypatch.setattr(project_broker, "open", lambda _: io.StringIO(configuration), raising=False)
    project_broker.initialize()
    identity = uuid4().hex
    body = json.dumps(
        {
            "deadline": time.time() + 90,
            "image_digest": policy["image_digest"],
            "files": {"base.txt": base64.b64encode(b"original").decode()},
            "argv": ["python", "-c", "import time; time.sleep(20); print('completed')"],
            "wall_seconds": 25,
        }
    ).encode()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            yield project_broker, identity, body, pool, start_entered, allow_start
        finally:
            allow_start.set()
            for name in names:
                docker("rm", "-f", name)
    for name in names:
        remaining = docker("ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}")
        assert remaining.returncode == 0 and not remaining.stdout.strip()
    assert project_broker.LIFECYCLES == {}


def state(identity):
    result = docker("inspect", "--format", "{{.State.Status}}", "agents-project-v3-" + identity)
    return result.stdout.strip().decode() if result.returncode == 0 else None


def persisted(module, identity):
    with sqlite3.connect(module.DB) as db:
        result = db.execute("SELECT result FROM attempts WHERE id=?", (identity,)).fetchone()[0]
    return json.loads(result)


def test_actual_cancel_after_create_before_start(broker):
    module, identity, body, pool, start_entered, allow_start = broker
    allow_start.clear()
    request = pool.submit(module.request, identity, body)
    assert start_entered.wait(10)
    assert state(identity) == "created"
    assert module.cancel(identity) == {"cancelled": True}
    assert state(identity) is None
    allow_start.set()
    expected = {"error": "sandbox_cancelled"}
    assert request.result(10) == expected
    assert persisted(module, identity) == expected
    assert module.request(identity, body) == expected
    assert state(identity) is None


def test_actual_cancel_running_container(broker):
    module, identity, body, pool, start_entered, _ = broker
    request = pool.submit(module.request, identity, body)
    assert start_entered.wait(10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and state(identity) != "running":
        assert not request.done(), request.result() if request.done() else None
        time.sleep(0.05)
    assert state(identity) == "running"
    assert module.cancel(identity) == {"cancelled": True}
    expected = {"error": "sandbox_cancelled"}
    assert request.result(10) == expected
    assert persisted(module, identity) == expected
    assert module.request(identity, body) == expected
    assert state(identity) is None
