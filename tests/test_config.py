from transfercore.config import Settings, normalize_database_url


def _write_settings(tmp_path, url: str) -> None:
    (tmp_path / "settings.toml").write_text(
        f'[database]\nurl="{url}"\n', encoding="utf-8"
    )


def test_database_url_env_overrides_settings_toml(monkeypatch, tmp_path):
    """The documented DATABASE_URL must win over the bundled settings.toml.

    Regression: the test suite used to write to the main database because the
    TOML file shadowed DATABASE_URL, letting the running API worker race the
    tests.
    """
    _write_settings(tmp_path, "postgresql+asyncpg://u:p@toml-host:5432/toml_db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:p@env-host:5432/env_db"
    )

    settings = Settings()

    assert settings.database.url == normalize_database_url(
        "postgresql://u:p@env-host:5432/env_db"
    )
    assert "env_db" in settings.database.url


def test_settings_toml_is_used_when_database_url_is_absent(
    monkeypatch, tmp_path
):
    _write_settings(tmp_path, "postgresql+asyncpg://u:p@toml-host:5432/toml_db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE__URL", raising=False)

    settings = Settings()

    assert settings.database.url.endswith("/toml_db")
