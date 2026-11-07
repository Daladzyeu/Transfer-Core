from typing import Any, Tuple, Type
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from pathlib import Path
import tomllib


class DatabaseConfig(BaseModel):
    url: str
    echo: bool


class TomlConfigSettingsSource(PydanticBaseSettingsSource):
    """
    Источник для чтения настроек из TOML-файла.
    """

    def get_field_value(self, field: Any, field_name: str) -> Tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # Вариант 1: Ищем settings.toml в корне проекта (на несколько уровней выше от этого файла)
        # Если этот файл лежит в src/orsha_geo_bot/config.py, то корень на 3 уровня выше:
        # Path(__file__).resolve().parent.parent.parent / "settings.toml"

        # Вариант 2 (более универсальный): Ищем от текущей рабочей директории (откуда запускаете скрипт)
        file_path = Path.cwd() / "settings.toml"

        # Если не нашли в текущей директории, можно попробовать найти относительно самого файла:
        if not file_path.exists():
            # Подстройте количество .parent в зависимости от глубины вложения вашего файла конфигурации
            file_path = Path(__file__).resolve().parent.parent.parent / "settings.toml"

        if not file_path.exists():
            return {}

        with file_path.open("rb") as f:
            return tomllib.load(f)


class Settings(BaseSettings):
    database: DatabaseConfig

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
        return (
            init_settings,
            TomlConfigSettingsSource(
                settings_cls
            ),  # Ставим TOML выше env, чтобы файл читал нормально
            env_settings,
        )
