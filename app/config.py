"""Application settings (spec 13.2)."""

import json
from functools import lru_cache
from typing import Annotated, Any

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
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_DATABASE: str = "pulky"
    DB_REQUIRE_SSL: bool = True
    DB_SSL_CA: str = ""
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
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}"
        )

    @property
    def db_connect_args(self) -> dict[str, Any]:
        if not self.DB_REQUIRE_SSL:
            return {}
        return {"ssl": _ssl_args(self.DB_SSL_CA)}


def _ssl_args(ca: str) -> dict[str, Any]:
    """PyMySQL ``ssl`` options: the system trust store, or this project's CA."""
    if not ca:
        return {"check_hostname": False}
    import os
    import tempfile

    if os.path.isfile(ca):
        return {"ca": ca, "check_hostname": True}
    pem = ca.replace("\\n", "\n")
    if "BEGIN CERTIFICATE" not in pem:
        raise ValueError("DB_SSL_CA is neither a PEM certificate nor a path to one")
    handle, path = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(handle, "w") as fh:
        fh.write(pem)
    return {"ca": path, "check_hostname": True}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
