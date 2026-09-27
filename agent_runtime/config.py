from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Deliberately no automatic dotenv loading. Only the opt-in smoke loads .env.local.
    model_config = SettingsConfigDict(extra="ignore")
    database_url: str = "postgresql+asyncpg://agents:local-development-only@localhost:5432/agents"
    database_schema: str = "public"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "agents-v1"
    api_key: SecretStr = SecretStr("")
    model_registry_file: str = "config/models.json"
    tool_registry_file: str = "config/tools.json"
    extension_registry_file: str = "config/extensions.json"
    openai_force_ipv4: bool = True
    approval_wait_seconds: int = Field(default=86400, ge=1, le=604800)
    max_active_runs: int = 20
    mcp_url: str = "http://localhost:8001/mcp"
    otel_exporter_otlp_endpoint: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()
