from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    environment: str = "development"
    secret_key: str = "dev-insecure-secret-change-me"
    # PostgreSQL only (psycopg 3 driver). Set the real password in .env.
    database_url: str = "postgresql+psycopg://mango_app:change-me@localhost:5432/mango_farm"
    admin_username: str = "admin"
    admin_password: str = ""
    public_base_url: str = ""
    enable_simulator: bool = True
    auto_migrate: bool = True
    seed_demo_data: bool = True
    conversation_timeout_minutes: int = 60
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    max_upload_bytes: int = 3 * 1024 * 1024

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_template_order_update: str = ""
    allow_unsigned_webhooks: bool = False

    # --- payments ---
    payment_provider: str = "cashfree"  # cashfree | mock (mock is refused in production)
    cashfree_client_id: str = ""
    cashfree_client_secret: str = ""
    cashfree_env: str = "sandbox"  # sandbox | production
    cashfree_api_version: str = ""  # empty -> DEFAULT_CASHFREE_API_VERSION
    payment_expiry_minutes: int = 30
    bcrypt_rounds: int = 12
    payment_release_on_failed_attempt: bool = False

    @field_validator("database_url")
    @classmethod
    def _postgres_only(cls, v: str) -> str:
        if not v.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must start with postgresql+psycopg:// (PostgreSQL is required)")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cashfree_configured(self) -> bool:
        return bool(self.cashfree_client_id and self.cashfree_client_secret)

    @property
    def cashfree_base_url(self) -> str:
        return ("https://api.cashfree.com/pg" if self.cashfree_env.lower() == "production"
                else "https://sandbox.cashfree.com/pg")

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
