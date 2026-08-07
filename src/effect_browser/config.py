from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from effect_browser.session import (
    DEFAULT_SESSION_RETENTION_HOURS,
    DEFAULT_SESSION_STATE_MAX_BYTES,
    MAX_SESSION_RETENTION_HOURS,
    MAX_SESSION_STATE_BYTES,
    MIN_SESSION_STATE_BYTES,
    decode_session_encryption_key,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EFFECT_BROWSER_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = "sqlite:///./effect-browser.db"
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )
    allowed_upload_roots: Annotated[tuple[Path, ...], NoDecode] = ()
    allowed_upload_origins: Annotated[tuple[str, ...], NoDecode] = ()
    provider: str = "auto"
    openai_model: str = "gpt-5.6"
    grok_model: str = "grok-4.5"
    mission_max_parallel_research: int = Field(default=4, ge=1, le=8)
    default_profile_id: UUID | None = None
    default_document_path: Path | None = None
    browser_executable: str | None = None
    browser_headless: bool = True
    browser_sandbox: bool = True
    artifacts_directory: Path = Path("artifacts")
    session_encryption_key: SecretStr | None = None
    session_state_max_bytes: int = Field(
        default=DEFAULT_SESSION_STATE_MAX_BYTES,
        ge=MIN_SESSION_STATE_BYTES,
        le=MAX_SESSION_STATE_BYTES,
    )
    session_retention_hours: int = Field(
        default=DEFAULT_SESSION_RETENTION_HOURS,
        ge=1,
        le=MAX_SESSION_RETENTION_HOURS,
    )
    default_tenant_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    default_actor_id: str = "local-operator"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value):
        if isinstance(value, str):
            return tuple(
                item.strip().rstrip("/") for item in value.split(",") if item.strip()
            )
        return value

    @field_validator("session_encryption_key", mode="before")
    @classmethod
    def validate_session_encryption_key(cls, value):
        if value is None or value == "":
            return None
        decode_session_encryption_key(value)
        return value

    @field_validator("allowed_upload_roots", mode="before")
    @classmethod
    def parse_upload_roots(cls, value):
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("allowed_upload_origins", mode="before")
    @classmethod
    def parse_upload_origins(cls, value):
        if isinstance(value, str):
            return tuple(
                item.strip().rstrip("/") for item in value.split(",") if item.strip()
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
