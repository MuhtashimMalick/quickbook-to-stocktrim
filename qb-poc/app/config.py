from functools import lru_cache
from pathlib import Path
from typing import Optional
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    qbwc_username: str
    qbwc_password: SecretStr
    admin_token: SecretStr
    qwc_owner_id: UUID
    qwc_file_id: UUID
    qwc_app_name: str = "QuickBooks StockTrim POC"
    qwc_app_description: str = "Proof of concept: read/write QuickBooks Desktop through qbwc-kit"
    app_url: str = "http://localhost:8000/qbwc"
    qwc_support_url: Optional[str] = None
    run_every_n_seconds: int = Field(600, ge=60)
    database_url: str = "sqlite:///./qb_poc.db"
    writes_enabled: bool = False
    max_qbxml_version: str = "16.0"
    page_size: int = Field(100, ge=1, le=1000)
    fixtures_dir: Path = Path("fixtures/qbxml")
    save_fixtures: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
