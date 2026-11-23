# Deploying the Pulky backend

Spec: `docs/spec/13-deploy.md` section 13.13 (the checklist lives there; this file gives the commands).

## Rules

- Deploy outside class hours. Live state is in process memory, so a deploy or restart ends every running game.
- Wire changes: ship the backend first, then the frontend, in the same window. An old frontend against a new backend means nobody can play.
- Never commit a secret. Treat the Firebase service-account JSON as a credential and rotate it if it is ever printed, logged or pasted.
- `render.yaml`, `.github/workflows/ci.yml` and this file are pinned by `tests/test_deploy/test_deploy_files.py` (CI-gated deploys, one instance while `REDIS_ENABLED` is `false`, no literal secret, the CI steps in order, the coverage floors). Change the test together with the file it pins.

## One-time setup

1. In Render, create a plain Docker web service `pulky-backend` from this repo on the free plan (not a Blueprint; `render.yaml` records the settings to match): one instance, health check `/api/v1/health`, and Auto-Deploy set to "After CI checks pass" in the dashboard. Free instances have no pre-deploy command, so migrations run at container startup: the Dockerfile CMD runs `alembic upgrade head` and then starts uvicorn. A failed migration exits the container, the new instance never becomes healthy and Render keeps the previous deploy live.
2. Set the variables marked `sync: false` in the Render dashboard: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE` (Pulky's own Neon PostgreSQL 18 project: user `neondb_owner`, port 5432, the database `pulkydb`; `DB_HOST` is Neon's **direct** endpoint `ep-….us-east-1.aws.neon.tech`, without `-pooler`, because the pooled endpoint runs PgBouncer in transaction mode and rejects the `options=-c timezone=UTC` startup parameter), `DB_SSL_CA` (the PEM text of the Let's Encrypt roots ISRG Root X1 and ISRG Root X2, which Neon's certificates chain to: the same public bundle for all four games, locally `<Novus>/novus-secrets/neon-ca.pem`, one line, so connections use `sslmode=verify-full`), `FIREBASE_SERVICE_ACCOUNT_JSON` (one line) and `REDIS_URL` (leave empty while `REDIS_ENABLED` is `false`).
   Neon Free plan, AWS US East 1 (the Render service's region): compute fixed at 0.25 CU, scaled to zero after 5 idle minutes (the first request after idle waits for a cold start of under a second; `pool_pre_ping` replaces the dropped connections), 100 CU-hours a month, 1 GB, `max_connections` 112. The restore window is short, so keep your own backups: `pg_dump -Fc --no-owner --no-privileges` against the direct endpoint.
   Connection budget: Pulky's whole budget is 20 connections, deploys included. Per instance at most `DB_POOL_SIZE + DB_MAX_OVERFLOW` = 7 + 2 = 9. During a deploy Render runs the old and new instance side by side (18), the new instance's startup `alembic upgrade head` takes 1 (19) and one operator `psql` session 1: 20. A backend run locally against Neon is one more instance and takes Pulky over budget during a deploy, so develop against a local Docker PostgreSQL 18 (`DB_HOST=localhost`, `DB_USER=postgres`, empty `DB_SSL_CA`, `DB_REQUIRE_SSL=false`).
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
- The database connection is encrypted and verified: the startup logs carry no DB TLS warning (`DB_SSL_CA` set, so `sslmode=verify-full`), and `psql`'s `\conninfo` shows `SSL Connection | true` (or, from this repo, `venv/bin/python ../../scripts/db/verify_live_db.py --expect-db pulkydb`). Do not use `pg_stat_ssl`: Neon's proxy terminates TLS, so it answers `f` even on an encrypted link.
- Startup logs show Firebase verification active, not the CRITICAL warning.
- Smoke test on the deployed stack, signed in and as a guest: create, join from a second browser, play a full day, finish a game.
