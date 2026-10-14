"""Entry point, CORS and auto-discovery (spec 13.2): AC10 and the app wiring."""

import ast
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import socketio
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://localhost:5173"


def test_application_wraps_the_fastapi_app_for_socketio() -> None:
    from app.config import settings
    from app.main import app, application

    assert isinstance(application, socketio.ASGIApp)
    assert app.title == settings.APP_NAME
    assert app.version == settings.VERSION


def test_handlers_package_is_imported_by_main() -> None:
    import app.main  # noqa: F401

    assert "app.sockets.handlers" in sys.modules


def test_cors_allows_a_configured_origin_with_credentials(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_refuses_an_unknown_origin(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_socketio_is_served_at_socket_io() -> None:
    from app.main import application

    with TestClient(application) as c:
        response = c.get("/socket.io/", params={"EIO": "4", "transport": "polling"})
    assert response.status_code == 200
    assert response.text.startswith("0{")  # the engine.io open packet


def test_routes_live_under_api_v1(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/health").status_code == 404


def test_all_routers_returns_the_health_router() -> None:
    from app.api.v1 import all_routers

    paths = {r.path for router in all_routers() for r in router.routes}
    assert "/health" in paths and "/health/deep" in paths


@pytest.fixture
def package_copy(tmp_path: Path) -> Path:
    """A private copy of ``app`` that a test may drop modules into."""
    target = tmp_path / "copy"
    target.mkdir()
    shutil.copytree(
        ROOT / "app", target / "app", ignore=shutil.ignore_patterns("__pycache__")
    )
    return target


def _python(copy: Path, code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=copy,
        env={**os.environ, "PYTHONPATH": str(copy)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip()


def test_a_dropped_in_router_is_served_without_editing_anything_else(
    package_copy: Path,
) -> None:
    (package_copy / "app/api/v1/dropin.py").write_text(textwrap.dedent("""
            from fastapi import APIRouter

            router = APIRouter()

            @router.get("/dropin")
            def dropin() -> dict:
                return {"dropped": True}
            """))
    out = _python(
        package_copy,
        """
        from fastapi.testclient import TestClient
        from app.main import app
        print(TestClient(app).get("/api/v1/dropin").json())
        """,
    )
    assert out == "{'dropped': True}"


def test_routers_are_collected_in_module_name_order(package_copy: Path) -> None:
    for name in ("aaa", "zzz"):
        (package_copy / f"app/api/v1/{name}.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            f"@router.get('/{name}')\n"
            "def f() -> dict:\n    return {}\n"
        )
    out = _python(
        package_copy,
        """
        from app.api.v1 import all_routers
        print([r.routes[0].path for r in all_routers()])
        """,
    )
    paths = ast.literal_eval(out)
    assert paths[0] == "/aaa"
    assert paths[-1] == "/zzz"
    assert paths.index("/aaa") < paths.index("/health") < paths.index("/zzz")


def test_a_dropped_in_handler_module_registers_its_events(package_copy: Path) -> None:
    (package_copy / "app/sockets/handlers/dropin.py").write_text(
        "from app.sockets.manager import sio\n\n"
        "@sio.event\n"
        "async def dropin_event(sid, data=None):\n    return None\n"
    )
    out = _python(
        package_copy,
        """
        import app.main
        from app.sockets.manager import sio
        print('dropin_event' in sio.handlers['/'])
        """,
    )
    assert out == "True"


def test_a_dropped_in_check_runs_at_startup(package_copy: Path) -> None:
    (package_copy / "app/core/checks/dropin_check.py").write_text(
        "import logging\n"
        "from app.core.checks import register_check\n\n"
        "@register_check\n"
        "def dropin_check() -> None:\n"
        "    logging.getLogger('dropin').warning('DROPIN-CHECK-RAN')\n"
    )
    out = _python(
        package_copy,
        """
        import logging, sys
        logging.basicConfig(stream=sys.stdout)
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app):
            pass
        """,
    )
    assert "DROPIN-CHECK-RAN" in out


def test_discovery_packages_work_while_empty(package_copy: Path) -> None:
    for package in ("app/api/v1", "app/sockets/handlers", "app/core/checks"):
        for module in (package_copy / package).glob("*.py"):
            if module.name != "__init__.py":
                module.unlink()
    out = _python(
        package_copy,
        """
        import app.sockets.handlers
        from app.api.v1 import all_routers
        from app.core.checks import run_startup_checks
        run_startup_checks()
        print(all_routers())
        """,
    )
    assert out == "[]"
