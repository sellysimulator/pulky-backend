"""Application settings (spec 13.2)."""

import json
import os
import tempfile
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "Pulky"
    VERSION: str = "0.1.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "https://pulkysim.web.app",
        "http://localhost:5173",
    ]
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""
    DB_DATABASE: str = "pulkydb"
    DB_REQUIRE_SSL: bool = True
    DB_SSL_CA: str = ""
    # Per instance at most 7 + 2 = 9 connections: a deploy runs the old and new
    # instance side by side, plus alembic and one psql, within Pulky's budget of
    # 20 connections on its Neon project (DEPLOY.md).
    DB_POOL_SIZE: int = 7
    DB_MAX_OVERFLOW: int = 2
    REDIS_ENABLED: bool = False
    REDIS_URL: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    @model_validator(mode="before")
    @classmethod
    def _empty_means_default(cls, values: Any) -> Any:
        """An empty value (the template's) means the default.

        Applied after the sources are merged, so an empty environment variable still
        shadows a dotenv value and the suite stays hermetic.
        """
        if isinstance(values, dict):
            return {k: v for k, v in values.items() if v != ""}
        return values

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _no_wildcard(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError(
                'CORS_ORIGINS must not contain "*": credentials are allowed'
            )
        return value

    @property
    def db_url(self) -> str:
        user = quote(self.DB_USER, safe="")
        password = quote(self.DB_PASSWORD, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}"
        )

    @property
    def db_connect_args(self) -> dict[str, Any]:
        """psycopg connection options: a connection attempt gives up after 10 s
        (``connect_timeout``, so an unreachable database fails a request instead of
        hanging it; no ``statement_timeout``, which a pooler may refuse as a startup
        option), the session time zone is UTC, and TLS is
        verified against the CA bundle in ``DB_SSL_CA`` when one is given
        (``verify-full``), else only required (``require``: encrypted, not
        verified). The ``options`` startup parameter needs Neon's direct
        endpoint: the pooled one (``-pooler``) rejects it."""
        args: dict[str, Any] = {"connect_timeout": 10, "options": "-c timezone=UTC"}
        if self.DB_SSL_CA:
            args |= {"sslmode": "verify-full", "sslrootcert": _ca_file(self.DB_SSL_CA)}
        elif self.DB_REQUIRE_SSL:
            args |= {"sslmode": "require"}
        return args


@lru_cache
def _ca_file(ca: str) -> str:
    """A path to the CA bundle: ``ca`` itself, or the PEM text written once to a
    temporary file (psycopg takes a file path, never the certificate text)."""
    pem = ca.replace("\\n", "\n")
    if "-----BEGIN" in pem:
        handle, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(handle, "w") as fh:
            fh.write(pem)
        return path
    if os.path.isfile(ca):
        return ca
    raise ValueError("DB_SSL_CA is neither a PEM certificate nor a path to one")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
