"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://waycore:waycore@localhost:5432/waycore"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 3600  # seconds — recycle connections after 1 hour
    db_echo: bool = False  # set True for SQL logging in development
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_provider: str = "anthropic"  # "anthropic" or "openai"
    llm_model: str | None = None  # override model name (e.g. "gpt-4o", "claude-sonnet-4-6")
    encryption_key: str  # Fernet key — generate with: Fernet.generate_key().decode()
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

    # Screenshot backend: "local" or "s3"
    screenshot_backend: str = "local"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "waycore-screenshots"


settings = Settings()
