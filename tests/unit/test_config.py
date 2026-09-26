from cineatelie.core.config import Settings


def test_defaults_load_without_an_env_file() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_env == "local"
    assert settings.worker_enabled is False


def test_cors_allowed_origins_list_splits_and_trims() -> None:
    settings = Settings(_env_file=None, CORS_ALLOWED_ORIGINS="https://a.com, https://b.com")
    assert settings.cors_allowed_origins_list == ["https://a.com", "https://b.com"]


def test_cors_allowed_origins_list_is_empty_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins_list == []
