# Pulky_Backend

## What this app is

The backend of Pulky, a multiplayer pulque-business simulation: FastAPI with a
python-socketio server mounted beside it (`app.main:application`), SQLAlchemy 2 +
PyMySQL for persistence, and live room state held in process memory or Redis.

## Read these instead of re-deriving

- The spec: `../docs/spec/` (start at `README.md` and `glossary.md`; backend layout
  and settings are `13-backend.md`, REST and database `13-rest-db.md`).
- The build plan: `../docs/plan/` (`00-conventions.md` first, then the section
  document for the file you are changing).

## Directory layout

```
app/main.py             FastAPI + socketio.ASGIApp -> `application`; lifespan checks
app/config.py           Settings (pydantic-settings)
app/core/               pure domain modules; checks/ and firebase.py are the impure ones
app/api/deps.py         verify_firebase_id_token, get_current_firebase_user
app/api/v1/             routers, auto-discovered
app/db/, app/models/    engine and sessions; Base and TimestampMixin
app/services/           state_backend.py and the services built on it
app/sockets/            manager.py (server + SocketManager), errors.py (@guarded), handlers/
tests/                  conftest.py holds the shared test doubles
```

## Commands

```bash
python3.11 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt
venv/bin/uvicorn app.main:application --reload --port 8080
venv/bin/pytest -m "not dbschema"
venv/bin/ruff check app tests
venv/bin/black --check app tests
venv/bin/mypy app
docker build -t pulky-backend .
```

Migrations are Alembic only (`alembic upgrade head`); never `create_all()` at startup.

## Configuration that matters

Settings are environment variables (a local dotenv file works): see the template file
`.env.example`, where an empty value means the default. `CORS_ORIGINS` never contains
`"*"`. `REDIS_ENABLED=false` means one process and live rooms lost on restart;
`true` needs `REDIS_URL`. `FIREBASE_SERVICE_ACCOUNT_JSON` unset logs a CRITICAL
message at startup and every signed-in user is rejected.

## Architecture rules not to break

- `app/core/` domain modules import only the standard library, pydantic and each other;
  none imports `app/core/firebase.py` or `app/core/checks/`.
- Reach the socket manager through `app.sockets.manager.get_socket_manager()`, the state
  backend through the module (`from app.services import state_backend` then
  `state_backend.get_state_backend()`, never `from app.services.state_backend import
  get_state_backend`) and Firebase verification through `app.api.deps.verify_firebase_id_token`
  at call time, never bound at import, because the section 01 fixtures monkeypatch the module attribute.
- Handlers that can fail are wrapped in `@guarded()`; errors never carry exception text.
- REST errors are `HTTPException` with a string `detail`; never return `str(e)`.
- Never log secrets, tokens, identities or payloads.

## Contracts that span both repos

- The wire events and error codes: spec `12-wire.md`. The frontend consumes them.
- The REST shapes and error details: spec `13-rest-db.md` section 13.4.
- End-of-game stats (spec `08-money.md` section 8.6) come from one builder, `app/core/stats.py`
  `build_stats`, for both `game_finished` and the results API. Each player's `flow` (the summary's flow
  chart) is built from the day records plus the free initial pipeline, which both callers pass from the
  game config (`initial_pipeline=lead_time_days * initial_pipeline_qty`); both identities of section 8.6
  hold by construction, and `tests/test_core/test_stats_flow.py` property-tests them.
- Shared fixtures in `../docs/plan/fixtures/` are copied unchanged into each repo's
  tests. The backend owns the engine; the frontend only renders what the server sends.
- **Hidden defaults** (spec 9.3): the four risk odds of `HIDDEN_DEFAULT_KEYS`
  (`app/core/defaults.py`) reach no host before the game ends. `room["config"]` is the
  host's config, null at each one left at its default; build the engine's config with
  `GameConfig.from_host_config` (never `GameConfig.from_dict(room["config"])`), and send
  only `room["config"]` to the host (`host_state` adds it; the engine's `host_view` has
  none). `validate_config` takes the host config and returns `host_config`; it skips the
  break-even warning while the failure chance or loss share is hidden. `game_finished`,
  the results and the stored `game_configs.config` carry the full config.
- **Limits are served, not copied**: the frontend reads `limits` from `GET
  /config/defaults` (`config_schema()`) and each field's bounds from its `fields`; a new
  limit a form needs goes in `LIMITS` (`app/core/limits.py`), never in the frontend.
- The Paid-price card's `tier_plans[].refund_value_today` comes from
  `insurance.shipment_unit_cost`, the function the engine prices shipments with (spec
  6.3); any change to how a shipment is priced changes both at once.

## Gotchas

- **[HARD-WON]** Build the venv with `python3.11 -m venv venv`, never a bare `python3`.
- **[HARD-WON]** `app/db/base.py` is `from ..models.base import Base`; `from .base import Base` imports itself.
- **[HARD-WON]** `TimestampMixin` columns need `server_default`, or raw INSERTs fail.
- **[HARD-WON]** Never hardcode `logger=True` / `engineio_logger=True` on the Socket.IO server.
- **[HARD-WON]** In a dotenv file the Firebase JSON goes in single quotes or none.
- **[HARD-WON]** `pytest.ini` scopes collection; pytest-asyncio 1.x fails unmarked async tests.
- Never list `asyncio` in a requirements file.
- `TestClient` runs the lifespan only as `with TestClient(app) as c:`.
