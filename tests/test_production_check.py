import pytest

from scripts.production_check import private_values, validate


@pytest.fixture
def values():
    return {
        "RUNWEAVE_IMAGE": "registry.example/runweave@sha256:" + "a" * 64,
        "API_KEY": "a-random-test-application-key-of-32-characters",
        "DATABASE_URL": "postgresql+asyncpg://app:unique-password@db.internal/app?ssl=require",
        "MIGRATION_DATABASE_URL": "postgresql+asyncpg://migrator:owner-password@db.internal/app?ssl=require",
        "TEMPORAL_ADDRESS": "temporal.internal:7233",
        "TEMPORAL_NAMESPACE": "runweave-production",
        "ARTIFACT_STORAGE_HOST_PATH": "/srv/runweave/artifacts",
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("RUNWEAVE_IMAGE", "runweave:latest"),
        ("API_KEY", "short"),
        ("MAX_ACTIVE_RUNS", "21"),
        ("DATABASE_URL", "postgresql+asyncpg://app:local-development-only@db/app?ssl=require"),
        ("DATABASE_URL", "postgresql+asyncpg://app:password@db/app"),
        ("TEMPORAL_ADDRESS", ""),
        ("E2B_API_KEY", "e2b_worker-only"),
    ],
)
def test_unsafe_deployment_config_is_rejected(values, key, value):
    values[key] = value
    with pytest.raises(ValueError):
        validate(values, {"E2B_API_KEY": "e2b_fake-test-key"})


def test_valid_config_is_read_only_and_worker_credentials_are_separate(values):
    validate(values, {"E2B_API_KEY": "e2b_fake-test-key"})
    with pytest.raises(ValueError):
        validate(values, {"E2B_API_KEY": "e2b_fake-test-key", "API_KEY": "misplaced"})


def test_credential_reader_rejects_symlinks_and_shared_permissions(tmp_path):
    file = tmp_path / "private.env"
    file.write_text("API_KEY=test-only\n")
    file.chmod(0o644)
    with pytest.raises(ValueError):
        private_values(file)
    file.chmod(0o600)
    assert private_values(file) == {"API_KEY": "test-only"}
    link = tmp_path / "link.env"
    link.symlink_to(file)
    with pytest.raises(OSError):
        private_values(link)
