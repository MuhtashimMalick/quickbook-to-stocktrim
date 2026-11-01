from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .tunnel_gate import QBWC_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    qbwc_username: str
    qbwc_password: SecretStr
    admin_token: SecretStr
    qwc_owner_id: UUID
    qwc_file_id: UUID
    qwc_app_name: str = "Specialty Archery StockTrim"
    qwc_app_description: str = "Specialty Archery: QuickBooks Desktop <-> StockTrim sync"
    qwc_filename: str = Field("SpecialtyArchery-StockTrim.qwc", pattern=r"^[A-Za-z0-9._-]+\.qwc$")
    app_url: str = "http://localhost:8000" + QBWC_PATH
    qwc_support_url: Optional[str] = None
    run_every_n_seconds: int = Field(600, ge=60)
    database_url: str = "sqlite:///./qb_poc.db"
    writes_enabled: bool = False
    max_qbxml_version: str = "16.0"
    page_size: int = Field(100, ge=1, le=1000)
    fixtures_dir: Path = Path("fixtures/qbxml")
    save_fixtures: bool = True
    log_dir: Path = Path("logs")
    log_retention_days: int = Field(14, ge=1)

    @field_validator("app_url")
    @classmethod
    def _check_app_url(cls, v: str) -> str:
        u = urlparse(v)
        if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))) or not u.path.endswith(QBWC_PATH):
            raise ValueError(f"app_url must be https (http only for localhost) and end with {QBWC_PATH}")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
