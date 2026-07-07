import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
import tempfile
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor the .env file to the repository root so settings resolve identically
# regardless of the process working directory (systemd units, tests run from
# subdirs, etc.) instead of a CWD-relative lookup that silently falls back to
# defaults.
_ENV_FILE = str(Path(__file__).resolve().parent.parent / ".env")


class SettingsBase(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

logger = logging.getLogger("dhis2_analyst.config")


class Settings(SettingsBase):
    deployment_mode: Literal["standalone", "dhis2", "combined"] = "standalone"

    dhis2_base_url: str = "https://play.dhis2.org/dev"
    dhis2_service_account_user: str = ""
    dhis2_service_account_pass: str = ""

    # mock | openai | anthropic | ollama | azure | cohere | gemini.
    # Defaults to "mock" so the app boots without external credentials (and so
    # importing the app never raises a bare ValidationError before the friendly
    # startup checks can run).
    llm_provider: str = "mock"
    llm_model: str = "gpt-4o"           # model string passed to LiteLLM
    llm_api_key: str = ""
    llm_base_url: str = ""              # for Ollama: http://localhost:11434
    llm_timeout_seconds: int = 30

    # Embedding model
    embedding_provider: str = "mock"    # mock | openai | cohere | ollama
    embedding_model: str = "text-embedding-3-small"

    # Tavily web enrichment
    tavily_api_key: str = ""
    tavily_endpoint: str = ""           # set for SearxNG-compatible self-hosted alternative
    tavily_trusted_domains: str = "who.int,cdc.gov,afro.who.int,unicef.org,.gov"

    # Feature flags
    evidence_fusion: bool = True
    audit_web_search: bool = True
    enable_direct_sql: bool = False

    # Storage and sessions
    database_url: str = "sqlite+aiosqlite:///./dhis2_analyst.db"
    redis_url: str = ""
    jwt_secret: str = "change-me-in-production"
    jwt_expire_seconds: int = 3600

    # File storage
    temp_file_dir: str = "/tmp/dhis2_analyst"
    max_file_age_seconds: int = 3600
    max_file_size_bytes: int = 52_428_800  # 50 MB

    # Rate limiting
    rate_limit_rpm: int = 60            # requests per minute per user/IP
    # Trust X-Forwarded-For for the client IP (enable ONLY behind a proxy you
    # control — clients can otherwise spoof the header to dodge rate limits).
    trust_proxy_headers: bool = False

    # CORS — include both Vite dev port and direct backend port
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:8000,http://localhost:8000"

    # Query safety
    metadata_confidence_threshold: float = 0.82
    max_rows: int = 10_000
    sql_timeout_seconds: int = 10

    @property
    def temp_path(self) -> Path:
        raw = self.temp_file_dir
        path = Path(raw)
        # Platform-aware: Unix-style /tmp/ path on Windows → use tempfile.gettempdir()
        if raw.startswith("/tmp/") and os.name == "nt":
            path = Path(tempfile.gettempdir()) / raw.split("/tmp/", 1)[1]
        elif raw.startswith("/tmp/") and not Path("/tmp").exists():
            path = Path(tempfile.gettempdir()) / raw.split("/tmp/", 1)[1]
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def trusted_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.tavily_trusted_domains.split(",") if d.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_postgres(self) -> bool:
        return "postgresql" in self.database_url or "postgres" in self.database_url



    def startup_checks(self) -> None:
        """Hard-fail on insecure or incomplete configuration.

        The ``mock`` provider is exempt: it runs entirely locally (deterministic
        classifier + canned generation) so it needs neither LLM credentials nor
        a production JWT secret, which is what makes the shipped ``.env.example``
        runnable out of the box.
        """
        if self.llm_provider == "mock":
            logger.warning(
                "llm_provider=mock — running with the deterministic local "
                "classifier and canned generation. Set a real LLM_PROVIDER for production."
            )
            return

        if not self.llm_api_key and not self.llm_base_url:
            logger.critical(
                "FATAL: No LLM credentials configured. "
                "Set LLM_API_KEY or LLM_BASE_URL (for Ollama) in .env."
            )
            sys.exit(1)

        if self.jwt_secret == "change-me-in-production":
            logger.critical(
                "FATAL: JWT_SECRET is still the default value. "
                "Set a strong, unique JWT_SECRET in .env before deploying."
            )
            sys.exit(1)

    def log_resolved_settings(self) -> None:
        """Log resolved non-secret settings at startup for observability."""
        logger.info(
            "settings_resolved",
            extra={
                "deployment_mode": self.deployment_mode,
                "llm_provider": self.llm_provider,
                "llm_model": self.llm_model,
                "embedding_provider": self.embedding_provider,
                "embedding_model": self.embedding_model,
                "evidence_fusion": self.evidence_fusion,
                "enable_direct_sql": self.enable_direct_sql,
                "database_type": "postgres" if self.is_postgres else "sqlite",
                "rate_limit_rpm": self.rate_limit_rpm,
                "max_rows": self.max_rows,
                "cors_origins": self.cors_origin_list,
                "temp_path": str(self.temp_path),
                "dhis2_base_url": self.dhis2_base_url,
                "has_tavily_key": bool(self.tavily_api_key),
                "has_dhis2_credentials": bool(self.dhis2_service_account_user),
            },
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
