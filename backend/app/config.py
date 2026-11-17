from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.models import NegativeStockPolicy, ProductKeyField
from .qbwc.gate import QBWC_PATH


class DatabaseSettings(BaseSettings):
    """Just the DB URL, so Alembic can migrate with only DATABASE_URL set."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str  # postgresql+psycopg://user:pass@host:5432/db (no default on purpose)

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("database_url must be a PostgreSQL URL, e.g. postgresql+psycopg://user:pass@host:5432/db")
        return v


class Settings(DatabaseSettings):
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
    writes_enabled: bool = False
    max_qbxml_version: str = "16.0"
    page_size: int = Field(100, ge=1, le=1000)
    fixtures_dir: Path = Path("fixtures/qbxml")
    save_fixtures: bool = True
    log_dir: Path = Path("logs")
    log_retention_days: int = Field(14, ge=1)
    # Product SKU = QuickBooks item Name (decided: unique, short, matches PO lines); FullName only if the client ever changes the rule
    product_key_field: ProductKeyField = "Name"
    # Negative QuickBooks stock: send as-is first (A13); clamp = send 0. Read by the push mapper.
    negative_stock_policy: NegativeStockPolicy = "as_is"
    # StockTrim (optional at start; StockTrimClient.from_settings raises StockTrimConfigError when a call needs them)
    stocktrim_base_url: str = "https://api.stocktrim.com"
    stocktrim_auth_id: Optional[SecretStr] = None
    stocktrim_auth_signature: Optional[SecretStr] = None
    # Scopes the BOM ledger and push runs to one StockTrim account. PRODUCTION DEPLOYMENTS MUST SET stocktrim_environment=production.
    stocktrim_environment: Literal["test", "production"] = "test"
    # Gate for every non-dry-run StockTrim push (the QuickBooks-side writes_enabled is separate)
    stocktrim_writes_enabled: bool = False
    stocktrim_max_rps: float = Field(5, gt=0, le=50)
    sales_history_years: int = Field(3, ge=1, le=20)  # sales-order push window: orders with TxnDate in the last N years
    stocktrim_concurrency: int = Field(4, ge=1, le=32)
    stocktrim_max_call_seconds: float = Field(90, ge=10, le=600)

    @field_validator("stocktrim_base_url")
    @classmethod
    def _check_stocktrim_url(cls, v: str) -> str:
        u = urlparse(v)
        if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))) or not u.hostname:
            raise ValueError("stocktrim_base_url must be https (http only for localhost): the API credentials travel in headers")
        return v

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
