# Deploying the Pulky backend

Spec: `docs/spec/13-deploy.md` section 13.13 (the checklist lives there; this file gives the commands).

## Rules

- Deploy outside class hours. Live state is in process memory, so a deploy or restart ends every running game.
- Wire changes: ship the backend first, then the frontend, in the same window. An old frontend against a new backend means nobody can play.
- Never commit a secret. Treat the Firebase service-account JSON as a credential and rotate it if it is ever printed, logged or pasted.
- `render.yaml`, `.github/workflows/ci.yml` and this file are pinned by `tests/test_deploy/test_deploy_files.py` (CI-gated deploys, one instance while `REDIS_ENABLED` is `false`, no literal secret, the CI steps in order, the coverage floors). Change the test together with the file it pins.

## One-time setup

1. In Render, create a plain Docker web service `pulky-backend` from this repo on the free plan (not a Blueprint; `render.yaml` records the settings to match): one instance, health check `/api/v1/health`, and Auto-Deploy set to "After CI checks pass" in the dashboard. Free instances have no pre-deploy command, so migrations run at container startup: the Dockerfile CMD runs `alembic upgrade head` and then starts uvicorn. A failed migration exits the container, the new instance never becomes healthy and Render keeps the previous deploy live.
2. Set the variables marked `sync: false` in the Render dashboard: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE` (the shared Novus Aiven PostgreSQL service's connection values: user `avnadmin` and the database `pulkydb`; `DB_HOST` is the service hostname), `DB_SSL_CA` (the service's CA certificate, the same one for all four games, one line, so connections use `sslmode=verify-full`), `FIREBASE_SERVICE_ACCOUNT_JSON` (one line) and `REDIS_URL` (leave empty while `REDIS_ENABLED` is `false`).
   Connection budget: all four games share one Aiven PostgreSQL 17 service (Free plan, 20 connections), each with its own database and role. Per instance at most `DB_POOL_SIZE + DB_MAX_OVERFLOW`: 2 + 1 = 3 for Selly, Pulky and Beery, 3 + 1 = 4 for Lemony. Normal running: 3 + 3 + 3 + 4 = 13. During a deploy Render runs the old and new instance of that game side by side (at most +4 = 17), the new instance's startup `alembic upgrade head` takes 1 and one operator `psql` session 1: 19 of 20. So deploy one game at a time. A backend run locally against the shared service is one more instance; prefer a local Docker PostgreSQL for development. On a bigger plan raise both variables (in the Render dashboard).
3. In GitHub, make sure the `CI` workflow (`.github/workflows/ci.yml`) runs on `main`; Render waits for it before deploying.
4. Deploy the frontend afterwards (see its `DEPLOY.md`).

## Before each deploy

Run the local gates from the repo root:

```
venv/bin/pytest -q -m "not dbschema" --cov=app
venv/bin/pytest -q -m dbschema --cov=app --cov-append
venv/bin/coverage report --fail-under=80
venv/bin/coverage report --include="app/core/*" --fail-under=95
venv/bin/ruff check app tests && venv/bin/black --check app tests && venv/bin/mypy app
```

After a migration change, also regenerate and commit the reviewed DDL (needs Docker; the `dbschema` suite fails until it matches):

```
python3 ../../scripts/db/gen_ddl.py --backend .
```

## Checklist (spec 13.13)

- Backend variables are set on Render, not only locally.
- The startup `alembic upgrade head` succeeded: the deploy logs show Alembic's upgrade before uvicorn starts, and the deploy went live.
- The database session is encrypted: `SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid();` answers `t`, and the startup logs carry no DB TLS warning.
- Startup logs show Firebase verification active, not the CRITICAL warning.
- Smoke test on the deployed stack, signed in and as a guest: create, join from a second browser, play a full day, finish a game.
