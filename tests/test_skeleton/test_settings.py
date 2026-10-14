"""Settings (spec 13.2): defaults, db_url, CORS validation, hermetic suite."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings, settings

SETTING_NAMES = [
    "APP_NAME",
    "VERSION",
    "DEBUG",
    "HOST",
    "PORT",
    "CORS_ORIGINS",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_DATABASE",
    "DB_REQUIRE_SSL",
    "DB_SSL_CA",
    "REDIS_ENABLED",
    "REDIS_URL",
    "FIREBASE_SERVICE_ACCOUNT_JSON",
]


@pytest.fixture
def pristine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No setting in the environment and no dotenv file in the working directory."""
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_equal_the_spec_table(pristine: None) -> None:
    s = Settings()
    assert s.APP_NAME == "Pulky"
    assert s.VERSION == "0.1.0"
    assert s.DEBUG is False
    assert s.HOST == "0.0.0.0"
    assert s.PORT == 8080
    assert s.CORS_ORIGINS == ["https://pulkysim.web.app", "http://localhost:5173"]
    assert s.DB_HOST == "localhost"
    assert s.DB_PORT == 3306
    assert s.DB_USER == "root"
    assert s.DB_PASSWORD == ""
    assert s.DB_DATABASE == "pulky"
    assert s.DB_REQUIRE_SSL is True
    assert s.DB_SSL_CA == ""
    assert s.REDIS_ENABLED is False
    assert s.REDIS_URL == ""
    assert s.FIREBASE_SERVICE_ACCOUNT_JSON == ""
    assert "*" not in s.CORS_ORIGINS


def test_db_url_worked_example() -> None:
    s = Settings(
        DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT=3306, DB_DATABASE="pulky"
    )
    assert s.db_url == "mysql+pymysql://u:p@h:3306/pulky"


def test_db_connect_args_follow_require_ssl() -> None:
    assert Settings(DB_REQUIRE_SSL=False).db_connect_args == {}
    args = Settings(DB_REQUIRE_SSL=True).db_connect_args
    assert isinstance(args["ssl"], dict)


def test_settings_ignore_unknown_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_UNRELATED_VARIABLE", "x")
    Settings()


def test_get_settings_is_cached_and_module_instance_exists() -> None:
    assert get_settings() is get_settings()
    assert isinstance(settings, Settings)


def test_wildcard_cors_is_rejected_by_keyword() -> None:
    """Failure mode 8: allow_credentials=True with '*' must never start."""
    with pytest.raises(ValidationError):
        Settings(CORS_ORIGINS=["*"])
    with pytest.raises(ValidationError):
        Settings(CORS_ORIGINS=["https://a.example", "*"])


def test_wildcard_cors_is_rejected_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", json.dumps(["*"]))
    with pytest.raises(ValidationError):
        Settings()


def test_suite_is_hermetic_from_a_dotenv_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC17: a .env in the working directory cannot change the defaults."""
    (tmp_path / ".env").write_text(
        "REDIS_ENABLED=true\n"
        'FIREBASE_SERVICE_ACCOUNT_JSON=\'{"type": "service_account"}\'\n'
        "DB_HOST=production.example\n"
    )
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.REDIS_ENABLED is False
    assert s.FIREBASE_SERVICE_ACCOUNT_JSON == ""
    assert s.DB_HOST == "localhost"
