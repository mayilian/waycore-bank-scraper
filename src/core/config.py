"""Application configuration via environment variables."""

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://waycore:waycore@localhost:5432/waycore"

    # Individual DB fields — used in ECS/CDK where Secrets Manager passes fields separately.
    # When set, these override database_url.
    db_host: str = ""
    db_port: str = "5432"
    db_username: str = ""
    db_password: str = ""
    db_name: str = "waycore"

    @model_validator(mode="after")
    def _build_database_url(self) -> "Settings":
        if self.db_host:
            self.database_url = (
                f"postgresql+asyncpg://{self.db_username}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return self

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 3600  # seconds — recycle connections after 1 hour
    db_echo: bool = False  # set True for SQL logging in development
    use_rds_proxy: bool = False  # set True when behind RDS Proxy — uses NullPool
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_provider: Literal["anthropic", "bedrock", "openai"] = "anthropic"
    llm_model: str | None = None  # override model name (e.g. "gpt-4o", "claude-sonnet-4-6")
    aws_region: str = "us-east-1"  # AWS region for Bedrock
    encryption_key: str  # Fernet key — generate with: Fernet.generate_key().decode()
    encryption_key_previous: str = ""  # old key — set during rotation, remove after re-encrypt
    restate_ingress_url: str = "http://localhost:8080"
    worker_port: int = 9000
    playwright_headful: bool = False
    screenshot_dir: str = "data/screenshots"

    # Browser stealth settings — override for locale/timezone matching
    browser_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    browser_locale: str = "en-US"
    browser_timezone: str = "America/New_York"

    # Operational caps — prevent runaway syncs
    max_sync_duration_secs: int = 600  # 10 minutes — hard cap per sync job
    max_pages_per_account: int = 50  # pagination limit per account
    max_llm_calls_per_sync: int = 100  # LLM API call budget per sync job
    max_concurrent_syncs: int = 5  # max simultaneous browser sessions per worker
    max_concurrent_per_bank: int = 3  # max simultaneous syncs against one bank_slug

    screenshot_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "waycore-screenshots"


settings = Settings()
