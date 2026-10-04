"""Validate the project sandbox image, or explicitly build and pin a local image."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class SetupError(Exception):
    pass


def docker_command():
    for command in (["docker"], ["sudo", "-n", "docker"]):
        try:
            result = subprocess.run(command + ["info"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return command
    raise SetupError("Docker daemon access is required (docker or sudo -n docker).")


def ensure_stopped(command, root):
    result = subprocess.run(
        command
        + [
            "ps",
            "--filter",
            f"label=com.docker.compose.project.working_dir={root.resolve()}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    if set(result.stdout.splitlines()) & {"api", "worker", "sandbox-broker"}:
        raise SetupError(
            "Drain and stop this checkout's API, worker and broker before changing image policy."
        )


def verify_image(command, digest):
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise SetupError("Project image must be an immutable sha256 Docker image ID.")
    result = subprocess.run(
        command + ["image", "inspect", digest, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode or result.stdout.strip() != digest:
        raise SetupError(
            "Configured project image is unavailable. For a fresh installation, run with --update-policy."
        )


def prepare(root=ROOT, *, update_policy=False, tag="runweave-project:local"):
    path = root / "config/general.json"
    if path.is_symlink() or not path.is_file():
        raise SetupError("Project policy must be a regular config/general.json file.")
    original = path.read_bytes()
    policy = json.loads(original)
    if not isinstance(policy, dict) or not DIGEST.fullmatch(str(policy.get("image_digest", ""))):
        raise SetupError("Project policy must contain an immutable sha256 image_digest.")
    command = docker_command()
    if not update_policy:
        verify_image(command, policy["image_digest"])
        return policy["image_digest"]

    ensure_stopped(command, root)
    with tempfile.TemporaryDirectory(prefix="runweave-project-image-") as directory:
        image_file = Path(directory) / "image-id"
        subprocess.run(
            command
            + [
                "build",
                "--file",
                "sandbox/Dockerfile.project",
                "--tag",
                tag,
                "--iidfile",
                str(image_file),
                ".",
            ],
            cwd=root,
            check=True,
            timeout=1200,
        )
        digest = image_file.read_text().strip()
    verify_image(command, digest)
    ensure_stopped(command, root)
    if path.is_symlink() or path.read_bytes() != original:
        raise SetupError("Project policy changed during the build; no policy was overwritten.")
    policy["image_digest"] = digest
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(json.dumps(policy, indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-policy",
        action="store_true",
        help="Build the project image and explicitly update local policy.",
    )
    parser.add_argument(
        "--tag", default="runweave-project:local", help="Local build tag; no image is pushed."
    )
    args = parser.parse_args(argv)
    try:
        digest = prepare(update_policy=args.update_policy, tag=args.tag)
    except SetupError as exc:
        print(str(exc))
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Project image setup failed; check Docker access and the project policy. No services restarted."
        )
        return 1
    print(f"Project image: {digest}")
    if args.update_policy:
        print(
            "Updated config/general.json. Start or rebuild services to apply it; no services were restarted."
        )
    else:
        print("Configured image is available. No policy or service changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
