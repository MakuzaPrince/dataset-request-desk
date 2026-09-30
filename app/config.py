"""Application settings, read once from environment variables."""
import logging
import os
import secrets
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def _secret_key() -> str:
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    # Fine for local dev/tests; tokens simply stop being valid after a restart.
    log.warning("SECRET_KEY not set; generating an ephemeral key")
    return secrets.token_urlsafe(48)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./desk.db"))
    secret_key: str = field(default_factory=_secret_key)
    token_ttl_minutes: int = field(default_factory=lambda: _env_int("TOKEN_TTL_MINUTES", 480))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    max_upload_bytes: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_BYTES", 200 * 1024 * 1024))


settings = Settings()
