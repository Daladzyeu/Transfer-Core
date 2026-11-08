import os
import socket
import tomllib
from pathlib import Path
from typing import Any, Tuple, Type
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def normalize_database_url(url: str) -> str:
    """Ensure asyncpg driver is specified and fallback to localhost if 'db' is unresolvable."""
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if "@db:" in url:
        try:
            socket.gethostbyname("db")
        except socket.gaierror:
            url = url.replace("@db:", "@localhost:", 1)
    return url


class DatabaseConfig(BaseModel):
    url: str
    echo: bool = False

    def __init__(self, **data: Any):
        if "url" in data and isinstance(data["url"], str):
            data["url"] = normalize_database_url(data["url"])
        super().__init__(**data)


class TomlConfigSettingsSource(PydanticBaseSettingsSource):
    """Source for reading settings from settings.toml."""

    def get_field_value(self, field: Any, field_name: str) -> Tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        file_path = Path.cwd() / "settings.toml"
        if not file_path.exists():
            file_path = Path(__file__).resolve().parent.parent.parent / "settings.toml"

        if not file_path.exists():
            return {}

        with file_path.open("rb") as f:
            data = tomllib.load(f)
            if "database" in data and "url" in data["database"]:
                data["database"]["url"] = normalize_database_url(data["database"]["url"])
            return data


class EnvDatabaseUrlSource(PydanticBaseSettingsSource):
    """Read the documented DATABASE_URL variable with high priority.

    ``settings.toml`` is a convenience default, not an override: deployment
    platforms and the test entrypoint set DATABASE_URL, and that value must
    win. Without this source pydantic-settings would let the TOML file shadow
    the environment variable.
    """

    def get_field_value(self, field: Any, field_name: str) -> Tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        url = os.environ.get("DATABASE_URL")
        if not url:
            return {}
        return {"database": {"url": normalize_database_url(url)}}


class Settings(BaseSettings):
    database: DatabaseConfig = DatabaseConfig(
        url=normalize_database_url(
            os.environ.get("DATABASE_URL")
            or "postgresql+asyncpg://transfer:transfer@localhost:5432/transfer"
        )
    )

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit constructor arguments, then the documented
        # DATABASE_URL variable, then nested env vars, then settings.toml.
        return (
            init_settings,
            EnvDatabaseUrlSource(settings_cls),
            env_settings,
            TomlConfigSettingsSource(settings_cls),
        )
