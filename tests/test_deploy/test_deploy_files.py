"""Section 20: the backend deploy files (render.yaml, ci.yml, DEPLOY.md).

Each rule is a checker that returns a list of problems. The real file must
yield none; a copy broken the way a failure mode describes must yield some,
so the checker is proved able to fail.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "render.yaml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY = ROOT / "DEPLOY.md"

SECRET_KEYS = [
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_DATABASE",
    "DB_SSL_CA",
    "FIREBASE_SERVICE_ACCOUNT_JSON",
    "REDIS_URL",
]
LITERAL_VARS = {
    "REDIS_ENABLED": "false",
    "DB_REQUIRE_SSL": "true",
    "DEBUG": "false",
    "CORS_ORIGINS": '["https://pulkysim.web.app"]',
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r'"private_key"\s*:'),
    re.compile(r'"type"\s*:\s*"service_account"'),
    re.compile(r"(?i)\bpassword\s*[:=]\s*\S{4,}"),
    re.compile(r"mysql\+?\w*://\S+:\S+@"),
    re.compile(r"rediss?://\S+:\S+@"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
]


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- render.yaml


def check_render(doc: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), list):
        return ["services missing"]
    services = doc["services"]
    if len(services) != 1:
        problems.append("expected exactly one service")
    svc = services[0] if services else {}
    expected = {
        "type": "web",
        "runtime": "docker",
        "name": "pulky-backend",
        "healthCheckPath": "/api/v1/health",
        "autoDeployTrigger": "checksPass",
        "preDeployCommand": "alembic upgrade head",
    }
    for key, value in expected.items():
        if svc.get(key) != value:
            problems.append(f"{key} must be {value!r}, got {svc.get(key)!r}")

    env = svc.get("envVars")
    if not isinstance(env, list):
        return problems + ["envVars missing"]
    by_key: dict[str, dict[str, Any]] = {}
    for entry in env:
        if entry.get("key") in by_key:
            problems.append(f"duplicate env var {entry.get('key')}")
        by_key[entry.get("key")] = entry

    for key, value in LITERAL_VARS.items():
        entry = by_key.get(key)
        if entry is None or entry.get("value") != value:
            problems.append(f"{key} must have the string value {value!r}")
    for key in SECRET_KEYS:
        entry = by_key.get(key)
        if entry is None:
            problems.append(f"{key} missing")
        elif entry.get("sync") is not False or "value" in entry:
            problems.append(f"{key} must be sync: false with no value")
    for key, entry in by_key.items():
        if key not in LITERAL_VARS and "value" in entry:
            problems.append(f"{key} carries a literal value")
        if key not in LITERAL_VARS and key not in SECRET_KEYS:
            if "generateValue" not in entry and "fromDatabase" not in entry:
                problems.append(f"unexpected env var {key}")

    instances = svc.get("numInstances")
    if instances != 1:
        problems.append(f"numInstances must be 1, got {instances!r}")
    redis_off = by_key.get("REDIS_ENABLED", {}).get("value") == "false"
    if isinstance(instances, int) and instances > 1 and redis_off:
        problems.append("several instances with in-memory live state")
    return problems


def test_render_yaml_exists_and_parses() -> None:
    assert RENDER.is_file()
    assert isinstance(load_yaml(RENDER), dict)


def test_render_yaml_meets_section_20_contract() -> None:
    assert check_render(load_yaml(RENDER)) == []


def test_render_yaml_has_no_secret_value_anywhere() -> None:
    text = RENDER.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), pattern.pattern


def test_ungated_deploys_are_detected() -> None:
    """Failure mode 1 (spec 14 #18): no checksPass means deploys skip CI."""
    for mutate in ("remove", "auto"):
        doc = copy.deepcopy(load_yaml(RENDER))
        if mutate == "remove":
            doc["services"][0].pop("autoDeployTrigger", None)
        else:
            doc["services"][0]["autoDeployTrigger"] = "commit"
        problems = check_render(doc)
        assert any("autoDeployTrigger" in p for p in problems), mutate


def test_multi_instance_with_memory_state_is_detected() -> None:
    """Failure mode 4 (spec 13.3): two instances would split the rooms."""
    doc = copy.deepcopy(load_yaml(RENDER))
    doc["services"][0]["numInstances"] = 2
    problems = check_render(doc)
    assert any("numInstances" in p for p in problems)
    assert any("in-memory" in p for p in problems)


def test_literal_secret_in_env_vars_is_detected() -> None:
    doc = copy.deepcopy(load_yaml(RENDER))
    for entry in doc["services"][0]["envVars"]:
        if entry["key"] == "DB_PASSWORD":
            entry.pop("sync", None)
            entry["value"] = "hunter2hunter2"
    assert any("DB_PASSWORD" in p for p in check_render(doc))


def test_wrong_literal_values_are_detected() -> None:
    doc = copy.deepcopy(load_yaml(RENDER))
    for entry in doc["services"][0]["envVars"]:
        if entry["key"] == "REDIS_ENABLED":
            entry["value"] = True  # a YAML boolean, not the string "false"
    assert any("REDIS_ENABLED" in p for p in check_render(doc))


# ---------------------------------------------------------------------- ci.yml

CI_STEPS = [
    "pip install -r requirements.txt -r requirements-dev.txt",
    "ruff check app tests",
    "black --check app tests",
    "mypy app",
    'pytest -m "not dbschema" --cov=app',
    "pytest -m dbschema --cov=app --cov-append",
    "coverage report --fail-under=80",
    'coverage report --include="app/core/*" --fail-under=95',
    "docker build .",
]


def _triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, list):
        return {name: None for name in raw}
    return raw or {}


def _run_lines(doc: dict[Any, Any]) -> list[str]:
    lines: list[str] = []
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            if "run" in step:
                lines.extend(
                    ln.strip() for ln in str(step["run"]).splitlines() if ln.strip()
                )
    return lines


def check_ci(doc: Any) -> list[str]:
    problems: list[str] = []
    triggers = _triggers(doc)
    for event in ("push", "pull_request"):
        if event not in triggers:
            problems.append(f"missing trigger {event}")
            continue
        cfg = triggers[event] or {}
        if "main" not in (cfg.get("branches") or []):
            problems.append(f"{event} must be limited to main")

    pythons = [
        str(step.get("with", {}).get("python-version"))
        for job in doc.get("jobs", {}).values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    if pythons != ["3.11"]:
        problems.append(f"python must be 3.11, got {pythons}")

    lines = _run_lines(doc)
    cursor = 0
    for wanted in CI_STEPS:
        for idx in range(cursor, len(lines)):
            if wanted in lines[idx]:
                cursor = idx + 1
                break
        else:
            problems.append(f"step missing or out of order: {wanted}")
    return problems


def test_ci_workflow_exists_and_parses() -> None:
    assert CI.is_file()
    assert isinstance(load_yaml(CI), dict)


def test_ci_runs_every_step_in_order_on_main_with_python_311() -> None:
    assert check_ci(load_yaml(CI)) == []


def test_ci_checks_out_the_repository() -> None:
    doc = load_yaml(CI)
    uses = [
        str(step.get("uses", ""))
        for job in doc["jobs"].values()
        for step in job["steps"]
    ]
    assert any(u.startswith("actions/checkout") for u in uses)


def test_ci_fails_when_a_coverage_floor_is_dropped() -> None:
    """Behaviour 1: a missed floor must fail CI, so both floors must be there."""
    for floor in ("--fail-under=80", "--fail-under=95"):
        doc = load_yaml(CI)
        for job in doc["jobs"].values():
            for step in job["steps"]:
                if "run" in step:
                    step["run"] = step["run"].replace(floor, "")
        problems = check_ci(doc)
        assert any("coverage report" in p for p in problems), floor


def test_ci_detects_wrong_python_and_wrong_order() -> None:
    doc = load_yaml(CI)
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/setup-python"):
                step["with"]["python-version"] = "3.12"
    assert any("3.11" in p for p in check_ci(doc))

    doc = load_yaml(CI)
    for job in doc["jobs"].values():
        job["steps"].reverse()
    assert check_ci(doc) != []


def test_ci_never_prints_a_secret() -> None:
    text = CI.read_text(encoding="utf-8")
    doc = load_yaml(CI)
    for line in _run_lines(doc):
        assert "secrets." not in line
        assert not re.match(r"^(printenv|env|set -x)\b", line)
        assert not re.match(r"^echo\b.*\$", line)
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), pattern.pattern


# ------------------------------------------------------------------- DEPLOY.md


def test_deploy_md_exists_and_points_at_the_spec_checklist() -> None:
    assert DEPLOY.is_file()
    text = DEPLOY.read_text(encoding="utf-8")
    assert "13.13" in text
    assert "render.yaml" in text
    assert "alembic upgrade head" in text


def test_deploy_md_states_the_operational_rules() -> None:
    text = DEPLOY.read_text(encoding="utf-8").lower()
    assert "class hours" in text
    assert "backend" in text and "frontend" in text
    assert re.search(r"backend[^.\n]*(first|before)", text)


def test_deploy_md_contains_no_secret_value() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), pattern.pattern
