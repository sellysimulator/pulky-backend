"""Files the skeleton ships: requirements, tool config, Docker, CLAUDE.md, template."""

import configparser
import importlib.metadata
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

RUNTIME_PINS = {
    "fastapi": "0.141.1",
    "uvicorn[standard]": "0.53.0",
    "python-socketio": "5.17.0",
    "pydantic": "2.13.5",
    "pydantic-settings": "2.15.0",
    "sqlalchemy": "2.0.54",
    "pymysql": "1.2.3",
    "alembic": "1.20.0",
    "firebase-admin": "7.6.0",
    "python-dotenv": "1.2.3",
    "httpx": "0.28.1",
    "redis": "8.1.0",
}
DEV_PINS = {
    "pytest": "9.1.1",
    "pytest-asyncio": "1.4.0",
    "pytest-cov": "7.1.0",
    "pytest-mock": "3.15.1",
    "hypothesis": "6.169.0",
    "black": "26.5.1",
    "ruff": "0.16.8",
    "mypy": "2.3.1",
    "testcontainers[mysql]": "4.15.0",
}


def _pins(name: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (ROOT / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        package, _, version = line.partition("==")
        pins[package.lower()] = version
    return pins


def test_requirements_are_exactly_the_spec_pins() -> None:
    assert _pins("requirements.txt") == RUNTIME_PINS
    assert _pins("requirements-dev.txt") == DEV_PINS


def test_neither_requirements_file_lists_asyncio() -> None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        packages = [p.split("[")[0].lower() for p in _pins(name)]
        assert "asyncio" not in packages


def test_installed_versions_match_the_pins() -> None:
    for package, version in {**RUNTIME_PINS, **DEV_PINS}.items():
        name = package.split("[")[0]
        assert importlib.metadata.version(name) == version, name


def test_pytest_ini_is_the_specified_one() -> None:
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini")
    section = parser["pytest"]
    assert section["testpaths"].strip() == "tests"
    assert section["pythonpath"].strip() == "."
    assert section["asyncio_mode"].strip() == "strict"
    assert section["asyncio_default_fixture_loop_scope"].strip() == "function"
    assert "dbschema" in section["markers"]


def test_pyproject_configures_black_ruff_and_mypy() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]
    assert config["black"]["line-length"] == 88
    assert "ruff" in config
    assert config["mypy"]["strict"] is False
    overrides = config["mypy"]["overrides"]
    core = [o for o in overrides if "app.core.*" in _as_list(o["module"])]
    assert core and core[0]["disallow_untyped_defs"] is True


def _as_list(value: object) -> list[str]:
    return [value] if isinstance(value, str) else list(value)  # type: ignore[call-overload]


def test_dockerignore_excludes_env_and_tests() -> None:
    lines = {
        ln.strip().rstrip("/")
        for ln in (ROOT / ".dockerignore").read_text().splitlines()
    }
    assert ".env" in lines or ".env*" in lines
    assert "tests" in lines


def test_gitignore_excludes_env_but_keeps_the_template() -> None:
    text = (ROOT / ".gitignore").read_text().splitlines()
    assert any(ln.strip() in (".env", ".env*") for ln in text)


def test_dockerfile_is_the_specified_one() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.11-slim" in text
    assert "COPY requirements.txt ." in text
    assert "--http h11" in text and "--proxy-headers" in text
    assert "app.main:application" in text
    assert "FORWARDED_ALLOW_IPS" in text


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _docker_available(), reason="no Docker daemon reachable")
def test_docker_image_builds() -> None:
    tag = "pulky-backend-skeleton-test"
    try:
        result = subprocess.run(
            ["docker", "build", "-t", tag, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert result.returncode == 0, result.stderr[-2000:]
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def test_claude_md_has_the_specified_headings_and_pointers() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    headings = [
        "What this app is",
        "Read these instead of re-deriving",
        "Directory layout",
        "Commands",
        "Configuration that matters",
        "Architecture rules not to break",
        "Contracts that span both repos",
        "Gotchas",
    ]
    positions = []
    for heading in headings:
        match = re.search(rf"^#+\s*{re.escape(heading)}\s*$", text, re.MULTILINE)
        assert match, heading
        positions.append(match.start())
    assert positions == sorted(positions)
    assert "python3.11 -m venv venv" in text
    assert "../docs/spec/" in text
    assert "../docs/plan/" in text


ENV_VARS = [
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


def _template_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_env_template_lists_every_variable_with_an_empty_value() -> None:
    values = _template_values()
    assert set(values) == set(ENV_VARS)
    assert all(v == "" for v in values.values())


def test_env_template_loads_to_the_defaults(tmp_path: Path) -> None:
    """Copying the template to a dotenv file must not break startup."""
    import os

    env = {k: v for k, v in os.environ.items() if k not in ENV_VARS}
    code = (
        "import sys, json;"
        "sys.path.insert(0, sys.argv[2]);"
        "from app.config import Settings;"
        "s = Settings(_env_file=sys.argv[1]);"
        "print(json.dumps([s.PORT, s.DEBUG, s.DB_REQUIRE_SSL, s.REDIS_ENABLED,"
        " s.CORS_ORIGINS]))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT / ".env.example"), str(ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip() == (
        '[8080, false, true, false, ["https://pulkysim.web.app",'
        ' "http://localhost:5173"]]'
    )


def test_no_core_module_imports_firebase_or_checks() -> None:
    """Behaviour 4: the pure core never reaches the impure modules."""
    pattern = re.compile(
        r"app\.core\.(firebase|checks)|from \.(firebase|checks)|"
        r"from \. import .*(firebase|checks)|from \.\. ?core"
    )
    core = ROOT / "app" / "core"
    for path in core.rglob("*.py"):
        relative = path.relative_to(core)
        if relative.parts[0] == "checks" or relative.name == "firebase.py":
            continue
        assert not pattern.search(path.read_text()), str(relative)
