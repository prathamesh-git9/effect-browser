from functools import lru_cache
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    provider: str = "deterministic"
    openai_model: str = "gpt-5.6"
    grok_model: str = "grok-4.5"
    browser_executable: str | None = None
    browser_headless: bool = True
    browser_sandbox: bool = True
    artifacts_directory: Path = Path("artifacts")
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

    @field_validator("allowed_upload_roots", mode="before")
    @classmethod
    def parse_upload_roots(cls, value):
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
