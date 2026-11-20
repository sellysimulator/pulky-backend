"""Settings (spec 13.2): defaults, db_url, CORS validation, hermetic suite."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

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
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
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
    assert s.DB_PORT == 5432
    assert s.DB_USER == "postgres"
    assert s.DB_PASSWORD == ""
    assert s.DB_DATABASE == "pulkydb"
    assert s.DB_REQUIRE_SSL is True
    assert s.DB_SSL_CA == ""
    assert s.DB_POOL_SIZE == 2
    assert s.DB_MAX_OVERFLOW == 1
    assert s.REDIS_ENABLED is False
    assert s.REDIS_URL == ""
    assert s.FIREBASE_SERVICE_ACCOUNT_JSON == ""
    assert "*" not in s.CORS_ORIGINS


def test_db_url_worked_example() -> None:
    s = Settings(
        DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT=5432, DB_DATABASE="pulky"
    )
    assert s.db_url == "postgresql+psycopg://u:p@h:5432/pulky"


def test_db_url_encodes_the_credentials() -> None:
    s = Settings(
        DB_USER="ana@pulky", DB_PASSWORD="p@ss:w/rd%#?", DB_HOST="h", DB_PORT=5432
    )
    assert s.db_url == (
        "postgresql+psycopg://ana%40pulky:p%40ss%3Aw%2Frd%25%23%3F@h:5432/pulkydb"
    )
    url = make_url(s.db_url)
    assert url.username == "ana@pulky" and url.password == "p@ss:w/rd%#?"


PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


def test_db_connect_args_set_utc_and_follow_tls_settings(tmp_path: Path) -> None:
    off = Settings(DB_REQUIRE_SSL=False, DB_SSL_CA="").db_connect_args
    assert off == {"options": "-c timezone=UTC"}
    required = Settings(DB_REQUIRE_SSL=True, DB_SSL_CA="").db_connect_args
    assert required == {"options": "-c timezone=UTC", "sslmode": "require"}

    ca = tmp_path / "ca.pem"
    ca.write_text(PEM)
    by_path = Settings(DB_SSL_CA=str(ca)).db_connect_args
    assert by_path == {
        "options": "-c timezone=UTC",
        "sslmode": "verify-full",
        "sslrootcert": str(ca),
    }


@pytest.mark.parametrize("pem", [PEM, PEM.replace("\n", "\\n")])
def test_a_pem_ca_is_written_once_to_a_readable_file(pem: str) -> None:
    """The PEM text (real newlines, or one dotenv line with literal \\n) is
    verified with ``verify-full`` from a file psycopg can read."""
    args = Settings(DB_REQUIRE_SSL=False, DB_SSL_CA=pem).db_connect_args
    assert args["sslmode"] == "verify-full"
    assert Path(args["sslrootcert"]).read_text() == PEM
    again = Settings(DB_SSL_CA=pem).db_connect_args
    assert again["sslrootcert"] == args["sslrootcert"]


def test_a_ca_that_is_neither_pem_nor_a_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DB_SSL_CA"):
        _ = Settings(DB_SSL_CA=str(tmp_path / "missing.pem")).db_connect_args


def test_pool_size_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "12")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    s = Settings()
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (12, 4)


def test_settings_ignore_unknown_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_UNRELATED_VARIABLE", "x")
    Settings()


def test_get_settings_is_cached_and_module_instance_exists() -> None:
    assert get_settings() is get_settings()
    assert isinstance(settings, Settings)


def test_wildcard_cors_is_rejected_by_keyword() -> None:
    """allow_credentials=True with '*' must never start."""
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
    """A .env in the working directory cannot change the defaults."""
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
