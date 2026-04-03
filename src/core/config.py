"""Application configuration via environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://waycore:waycore@localhost:5432/waycore"

    # Individual DB fields — used in ECS/CDK where Secrets Manager passes fields separately.
    # When set, these override database_url.
    db_host: str = ""
    db_port: int = 5432
    db_username: str = ""
    db_password: SecretStr = SecretStr("")
    db_name: str = "waycore"

    @model_validator(mode="after")
    def _build_database_url(self) -> "Settings":
        if self.db_host:
            self.database_url = (
                f"postgresql+asyncpg://{self.db_username}:{self.db_password.get_secret_value()}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return self

    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=50)
    db_pool_recycle: int = 3600  # seconds — recycle connections after 1 hour
    db_echo: bool = False  # set True for SQL logging in development
    use_rds_proxy: bool = False  # set True when behind RDS Proxy — uses NullPool
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_provider: Literal["anthropic", "bedrock", "openai"] = "anthropic"
    llm_model: str | None = None  # override model name (e.g. "gpt-4o", "claude-sonnet-4-6")
    aws_region: str = "us-east-1"  # AWS region for Bedrock
    encryption_key: SecretStr  # Fernet key — generate with: Fernet.generate_key().decode()
    encryption_key_previous: SecretStr = SecretStr("")  # old key for rotation
    restate_ingress_url: str = "http://localhost:8080"
    worker_port: int = Field(default=9000, ge=1, le=65535)
    playwright_headful: bool = False
    screenshot_dir: Path = Path("data/screenshots")

    # Browser stealth settings — override for locale/timezone matching
    browser_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    browser_locale: str = "en-US"
    browser_timezone: str = "America/New_York"

    # Operational caps — prevent runaway syncs
    max_sync_duration_secs: int = Field(default=600, ge=1, le=3600)
    max_pages_per_account: int = Field(default=50, ge=1, le=500)
    max_llm_calls_per_sync: int = Field(default=100, ge=1, le=500)
    max_concurrent_syncs: int = Field(default=5, ge=1, le=50)
    max_concurrent_per_bank: int = Field(default=3, ge=1, le=20)

    screenshot_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_bucket: str = "waycore-screenshots"


settings = Settings()
