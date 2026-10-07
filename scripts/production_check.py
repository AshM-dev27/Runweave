"""Read-only configuration validation for the single-workspace E2B deployment."""

import argparse
import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import dotenv_values


def private_values(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Credential files must be regular files with permissions 0600")
        return dotenv_values(stream=source, interpolate=False)


def validate(values, worker):
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", values.get("RUNWEAVE_IMAGE") or ""):
        raise ValueError("RUNWEAVE_IMAGE must use an immutable sha256 digest")
    key = values.get("API_KEY") or ""
    if len(key) < 32 or key.lower().startswith(("replace", "example")):
        raise ValueError("API_KEY must be a random application secret of at least 32 characters")
    url = urlsplit(values.get("DATABASE_URL") or "")
    if url.scheme != "postgresql+asyncpg" or not url.hostname or not url.username or not url.password:
        raise ValueError("DATABASE_URL requires an authenticated PostgreSQL asyncpg URL")
    if unquote(url.password) == "local-development-only":
        raise ValueError("Development database credentials cannot be used for production")
    if parse_qs(url.query).get("ssl") not in [["require"], ["verify-full"]]:
        raise ValueError("DATABASE_URL must explicitly require TLS with ssl=require or ssl=verify-full")
    migration = urlsplit(values.get("MIGRATION_DATABASE_URL") or "")
    if (
        migration.scheme != "postgresql+asyncpg"
        or not migration.password
        or (migration.hostname, migration.port, migration.path) != (url.hostname, url.port, url.path)
        or migration.username == url.username
        or unquote(migration.password) == "local-development-only"
        or parse_qs(migration.query).get("ssl") not in [["require"], ["verify-full"]]
    ):
        raise ValueError(
            "MIGRATION_DATABASE_URL requires a separate owner role on the same database with TLS"
        )
    if not values.get("TEMPORAL_ADDRESS") or not values.get("TEMPORAL_NAMESPACE"):
        raise ValueError("Configure a private Temporal address and a dedicated namespace")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", values.get("DATABASE_SCHEMA", "public")):
        raise ValueError("DATABASE_SCHEMA must be a simple PostgreSQL identifier")
    try:
        active = int(values.get("MAX_ACTIVE_RUNS", "20"))
    except (ValueError, TypeError):
        raise ValueError("MAX_ACTIVE_RUNS must be an integer") from None
    if not 1 <= active <= 20:
        raise ValueError("This deployment profile permits at most 20 active runs")
    if not Path(values.get("ARTIFACT_STORAGE_HOST_PATH") or "").is_absolute():
        raise ValueError(
            "ARTIFACT_STORAGE_HOST_PATH must be an absolute persistent directory shared by API and workers"
        )
    try:
        file_bytes = int(values.get("ARTIFACT_MAX_BYTES", "16777216"))
        storage_bytes = int(values.get("ARTIFACT_STORAGE_BYTES", "536870912"))
        run_bytes = int(values.get("ARTIFACT_RUN_BYTES", "67108864"))
    except (ValueError, TypeError):
        raise ValueError("Artifact limits must be integer byte counts") from None
    if (
        not 262144 <= file_bytes <= 268435456
        or storage_bytes < max(file_bytes, 33554432)
        or run_bytes < max(file_bytes, 1048576)
    ):
        raise ValueError(
            "Artifact limits must allow each file within run/storage quotas and a maximum of 256 MiB per file"
        )
    if not (worker.get("E2B_API_KEY") or "").startswith("e2b_"):
        raise ValueError("The worker credential file must contain E2B_API_KEY")
    if "API_KEY" in worker:
        raise ValueError("Keep application authentication out of the worker credential file")
    if values.get("E2B_API_KEY"):
        raise ValueError("Keep E2B_API_KEY exclusively in the worker credential file")


def main(path):
    values = private_values(path)
    worker_path = Path(values.get("WORKER_ENV_FILE") or "")
    if not worker_path.is_absolute():
        raise ValueError("WORKER_ENV_FILE must be an absolute path")
    validate(values, private_values(worker_path))
    print(
        "Configuration passed: immutable image, private credential files, PostgreSQL TLS, bounded admission."
    )
    print(
        "No services changed or provider calls made. Verify namespace isolation, backups, TLS ingress and readiness before admission."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        main(args.env_file)
    except ValueError as error:
        print(str(error))
        raise SystemExit(1) from None
    except Exception:
        print("Configuration could not be read; check the private file paths and permissions.")
        raise SystemExit(1) from None
