from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Security
    SECRET_KEY: str = "change-this-secret-key-in-production-please"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # Database
    DATABASE_URL: str = "sqlite:///./checkin.db"

    # GoHighLevel
    GHL_API_KEY: str = ""
    GHL_LOCATION_ID: str = ""
    GHL_API_BASE_URL: str = "https://services.leadconnectorhq.com"
    GHL_API_VERSION: str = "2021-07-28"

    # Application
    BASE_URL: str = "http://localhost:8000"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme123"
    QR_CODE_DIR: str = "static/qr_codes"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
