from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """12-factor application configuration shared across services."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'sovereign.db'}"
    redis_url: str = ""

    mcp_api_key: str = "dev-mcp-internal-key"
    admin_api_key: str = "dev-admin-secret-key"

    erp_mcp_url: str = "http://127.0.0.1:8101/mcp"
    erp_admin_url: str = "http://127.0.0.1:8101"
    knowledge_mcp_url: str = "http://127.0.0.1:8102/mcp"

    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000

    llm_mode: str = "offline"  # "offline" | "vertex_adc" | "litellm"
    primary_model: str = "gemini-3.8-flash"
    fallback_model: str = "gemini-3.5-flash-lite"
    vertex_project_id: str = "gen-lang-client-0430700811"
    vertex_location: str = "global"
    google_application_credentials: str = ""
    ollama_api_base: str = "http://127.0.0.1:11434"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2

    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    tenants_config_path: str = str(PROJECT_ROOT / "configs" / "tenants.json")
    policies_config_path: str = str(PROJECT_ROOT / "configs" / "policies.yaml")
    mcp_servers_config_path: str = str(PROJECT_ROOT / "configs" / "mcp_servers.json")
    sops_dir: str = str(PROJECT_ROOT / "dataset" / "sops")
    traces_dir: str = str(PROJECT_ROOT / "data" / "traces")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
