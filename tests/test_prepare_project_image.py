import json
import subprocess
from pathlib import Path

import pytest

from scripts import prepare_project_image as setup

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "config/general.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"policy_version": 3, "image_digest": OLD, "other": {"limit": 42}}))
    return tmp_path, path


def mock_docker(monkeypatch, *, running="", digest=NEW, build_error=False, inspect_missing=False, hook=None):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "info":
            return subprocess.CompletedProcess(command, 0)
        if command[1] == "ps":
            return subprocess.CompletedProcess(command, 0, stdout=running)
        if command[1] == "image":
            return subprocess.CompletedProcess(command, int(inspect_missing), stdout=command[3])
        assert command[1] == "build"
        if build_error:
            raise subprocess.CalledProcessError(1, command)
        Path(command[command.index("--iidfile") + 1]).write_text(digest)
        if hook:
            hook()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    return calls


def test_validation_does_not_build_or_change_policy(monkeypatch, policy):
    root, path = policy
    original = path.read_bytes()
    calls = mock_docker(monkeypatch)
    assert setup.prepare(root) == OLD
    assert path.read_bytes() == original
    assert [c[1] for c in calls] == ["info", "image"]


def test_missing_image_leaves_policy_unchanged(monkeypatch, policy):
    root, path = policy
    original = path.read_bytes()
    mock_docker(monkeypatch, inspect_missing=True)
    with pytest.raises(setup.SetupError, match="--update-policy"):
        setup.prepare(root)
    assert path.read_bytes() == original


def test_explicit_build_pins_image_id_and_retains_other_policy(monkeypatch, policy):
    root, path = policy
    calls = mock_docker(monkeypatch)
    assert setup.prepare(root, update_policy=True, tag="runweave-test-project:local") == NEW
    assert json.loads(path.read_text()) == {"policy_version": 3, "image_digest": NEW, "other": {"limit": 42}}
    build = next(c for c in calls if c[1] == "build")
    assert build[build.index("--tag") + 1] == "runweave-test-project:local"
    assert not any("restart" in c or "up" in c or "push" in c for c in calls)


@pytest.mark.parametrize("service", ["api", "worker", "sandbox-broker"])
def test_running_services_prevent_build_and_policy_update(monkeypatch, policy, service):
    root, path = policy
    original = path.read_bytes()
    calls = mock_docker(monkeypatch, running=service + "\n")
    with pytest.raises(setup.SetupError, match="Drain and stop"):
        setup.prepare(root, update_policy=True)
    assert path.read_bytes() == original
    assert not any(c[1] == "build" for c in calls)


@pytest.mark.parametrize("kind", ["build_failure", "invalid_id", "missing_image"])
def test_failed_build_validation_does_not_change_policy(monkeypatch, policy, kind):
    root, path = policy
    original = path.read_bytes()
    mock_docker(
        monkeypatch,
        build_error=kind == "build_failure",
        digest="mutable:latest" if kind == "invalid_id" else NEW,
        inspect_missing=kind == "missing_image",
    )
    with pytest.raises((setup.SetupError, subprocess.CalledProcessError)):
        setup.prepare(root, update_policy=True)
    assert path.read_bytes() == original


def test_policy_edit_during_build_is_not_overwritten(monkeypatch, policy):
    root, path = policy
    changed = json.dumps({"image_digest": OLD, "operator_change": True})
    mock_docker(monkeypatch, hook=lambda: path.write_text(changed))
    with pytest.raises(setup.SetupError, match="changed during"):
        setup.prepare(root, update_policy=True)
    assert path.read_text() == changed


def test_service_started_during_build_prevents_policy_update(monkeypatch, policy):
    root, path = policy
    original = path.read_bytes()
    mock_docker(monkeypatch, hook=lambda: mock_docker(monkeypatch, running="worker\n"))
    with pytest.raises(setup.SetupError, match="Drain and stop"):
        setup.prepare(root, update_policy=True)
    assert path.read_bytes() == original


def test_symlink_policy_is_rejected_before_docker(monkeypatch, policy):
    root, path = policy
    actual = path.with_name("actual.json")
    path.rename(actual)
    path.symlink_to(actual)
    calls = mock_docker(monkeypatch)
    with pytest.raises(setup.SetupError, match="regular"):
        setup.prepare(root, update_policy=True)
    assert calls == []


def test_docker_fallback_is_noninteractive(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0 if command[0] == "sudo" else 1)

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.docker_command() == ["sudo", "-n", "docker"]
    assert calls == [["docker", "info"], ["sudo", "-n", "docker", "info"]]
