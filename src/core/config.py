"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://waycore:waycore@localhost:5432/waycore"
    anthropic_api_key: str
    encryption_key: str  # Fernet key — generate with: Fernet.generate_key().decode()
    restate_ingress_url: str = "http://localhost:8080"
    playwright_headful: bool = False
    screenshot_dir: str = "data/screenshots"

    # Screenshot backend: "local" or "s3"
    screenshot_backend: str = "local"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "waycore-screenshots"


settings = Settings()
