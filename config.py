import logging
import re
import secrets

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    discogs_token: str
    discogs_username: str
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:latest"
    app_name: str = "DiscogsRecommender/1.0"
    cache_ttl_seconds: int = 3600
    max_thumbs_entries: int = 500
    max_cache_entries: int = 1000
    secret_key: str = ""

    class Config:
        env_file = ".env"

    @field_validator("discogs_username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("discogs_username must be 1-100 characters")
        if not re.match(r"^[a-zA-Z0-9._-]+$", v):
            raise ValueError("discogs_username contains invalid characters")
        return v

    @field_validator("discogs_token")
    @classmethod
    def validate_discogs_token(cls, v: str) -> str:
        if not v or len(v) < 10:
            raise ValueError("discogs_token appears invalid")
        return v

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_anthropic_key(cls, v: str) -> str:
        if v and not v.startswith("sk-ant-"):
            raise ValueError("anthropic_api_key must start with 'sk-ant-'")
        return v

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if not v:
            v = secrets.token_hex(32)
            logger.warning(
                "SECRET_KEY not set in .env — generated random key. "
                "Sessions will not survive restarts."
            )
        return v


def _load_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        raise


settings = _load_settings()
