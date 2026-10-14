# Deploying the Pulky backend

Spec: `docs/spec/13-deploy.md` section 13.13 (the checklist lives there; this file gives the commands).

## Rules

- Deploy outside class hours. Live state is in process memory, so a deploy or restart ends every running game.
- Wire changes: ship the backend first, then the frontend, in the same window. An old frontend against a new backend means nobody can play.
- Never commit a secret. Treat the Firebase service-account JSON as a credential and rotate it if it is ever printed, logged or pasted.

## One-time setup

1. In Render, create a Blueprint from this repo (`render.yaml`). It makes the Docker web service `pulky-backend` with one instance, health check `/api/v1/health`, `autoDeployTrigger: checksPass` and the pre-deploy command `alembic upgrade head`.
2. Set the variables marked `sync: false` in the Render dashboard: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE`, `DB_SSL_CA`, `FIREBASE_SERVICE_ACCOUNT_JSON` (one line) and `REDIS_URL` (leave empty while `REDIS_ENABLED` is `false`).
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

## Checklist (spec 13.13)

- Backend variables are set on Render, not only locally.
- The pre-deploy `alembic upgrade head` succeeded.
- Startup logs show Firebase verification active, not the CRITICAL warning.
- Smoke test on the deployed stack, signed in and as a guest: create, join from a second browser, play a full day, finish a game.
