# Pulky Backend

FastAPI + Socket.IO backend for Pulky, the multiplayer pulque supply game. The server owns
the game: a pure engine (`app/core/`) plays every day, the rooms live in a state backend
(process memory, or Redis when switched on), and a finished game is stored in PostgreSQL
(SQLAlchemy 2, psycopg 3, Alembic). Identity is a Firebase ID token or a guest id, checked at
the Socket.IO handshake and on the REST routes that need it.

The game, the wire and the architecture are specified in `../docs/spec/` (start at its
`README.md`; the backend is `13-backend.md`, REST and database `13-rest-db.md`, the wire
`12-wire.md`). `CLAUDE.md` gives the working rules for changing this repo, and `DEPLOY.md`
the Render deploy.

## Run

```bash
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env     # DB_* for a local PostgreSQL 18; FIREBASE_SERVICE_ACCOUNT_JSON for Google sign-in
venv/bin/alembic upgrade head
venv/bin/uvicorn app.main:application --reload --port 8080
```

Every setting has a default, and an empty value in the dotenv file means that default
(spec §13.2, the template lists them all). Develop against a local Docker PostgreSQL 18
without TLS (`DB_REQUIRE_SSL=false`), never against Neon: a local backend there breaks the
connection budget in `DEPLOY.md`. Without `FIREBASE_SERVICE_ACCOUNT_JSON` the app starts, logs a
CRITICAL message, and accepts only guests. `REDIS_ENABLED=false` (the default) keeps the
live rooms in this one process: run a single worker, and a restart ends every running game.

## Tests and gates

The suite never reads a developer's dotenv file. The tests marked `dbschema` run against a
throwaway `postgres:18` through Testcontainers with `alembic upgrade head` applied, so they
need a running Docker daemon. CI (`.github/workflows/ci.yml`) runs exactly these, then builds
the image; Render deploys only after it passes.

```bash
venv/bin/ruff check app tests
venv/bin/black --check app tests
venv/bin/mypy app
venv/bin/pytest -m "not dbschema" --cov=app
venv/bin/pytest -m dbschema --cov=app --cov-append
venv/bin/coverage report --fail-under=80
venv/bin/coverage report --include="app/core/*" --fail-under=95
```

The schema is one Alembic revision (`alembic/versions/0001_postgres_baseline.py`). Its DDL is
committed as `db/schema.postgres.sql` and regenerated with
`python3 ../../scripts/db/gen_ddl.py --backend .` (Docker) after a migration change; the
`dbschema` suite fails while the models, the migrations and that file disagree. Migrations
are never run by the app itself: the container runs `alembic upgrade head` before uvicorn
(Dockerfile CMD).

The balance harness plays scripted strategies on the real engine and checks the balance
targets of spec §13.14; rerun it after changing a default:

```bash
venv/bin/python -m app.tools.balance --seeds 1000 --players 4 [--strategy NAME ...] [--json]
```

## Layout

| Path | What it does |
|---|---|
| `app/main.py` | FastAPI app with the Socket.IO server mounted beside it (`application`), the abuse and CORS middleware, the routers under `/api/v1`, and the startup checks in the lifespan. |
| `app/config.py` | Settings (pydantic-settings), spec §13.2. |
| `app/core/` | The game, pure: no I/O, no clock, no logging, randomness only from `rng.stream(seed, day, purpose)`. `engine.py` (`PulkyEngine`) drives one game; `records.py` holds the shapes every module and the wire share (`Decision`, `DayRecord`, `MarketDay`, ...); `defaults.py` and `limits.py` hold every default and hard limit; the rules are in `lots`, `supply`, `peer_market`, `futures`, `transport`, `insurance`, `demand`, `reputation`, `settlement` and `stats`; `previews.py` computes every figure of the host config screen. |
| `app/core/abuse.py`, `checks/`, `firebase.py` | The impure parts of `core`: abuse limits (spec §0.6: rate limiter, client IP, REST middleware, socket caps), the startup checks (they only log), and the lazy firebase-admin init. |
| `app/api/deps.py` | The one token-verification helper, used by REST and the handshake, and `get_current_firebase_user`. |
| `app/api/v1/` | Routers, auto-discovered: `health`, `rooms`, `config`, `games`, `users`. |
| `app/services/state_backend.py`, `state_service.py` | The live-state store (in memory or Redis) and the room document on top of it: lock, schema guard, `seq`. |
| `app/services/room_service.py`, `game_service.py`, `play_service.py` | Lobby and config rules; start, resync and the player and host payloads; submit, resolve, end early and finish. Every mutation runs under the room lock; emits and persistence happen after it. |
| `app/services/room_turn.py` | A turn on a room: its lock, and its events sent after it in the order of their `seq`. |
| `app/services/db_service.py`, `results_service.py`, `stats_service.py`, `user_service.py`, `claim_service.py`, `export_service.py` | End-of-game persistence (one idempotent transaction keyed by the game id), the stored results, profile stats, users, the guest-results claim, and the CSV/JSON export. |
| `app/models/`, `app/db/`, `app/schemas/` | SQLAlchemy models, engine and sessions; the REST response models. |
| `app/sockets/` | `manager.py` (the server, plus the Redis manager when Redis is on), `errors.py` (`@guarded`) and `handlers/` (`connection`, `lobby`, `play`, `host`), auto-discovered. |
| `app/tools/balance.py` | The balance harness CLI. |
| `tests/` | One folder per area (`test_core`, `test_services`, `test_sockets`, `test_api`, `test_models`, `test_deploy`, `test_skeleton`, `test_tools`); shared doubles in `conftest.py`, shared fixtures and data in `fixtures/`. |

## REST (`/api/v1`)

| Route | What it does |
|---|---|
| `GET /health`, `GET /health/deep` | Liveness; the deep check reports the database, Redis and the state backend. |
| `GET /config/defaults` | The default config, field metadata and every limit the client shows (`limits`). |
| `POST /config/validate`, `POST /config/preview` | The verdict on a draft config, and every figure the host config screen shows. |
| `POST /rooms/create`, `POST /rooms/batch-create` | Create one room, or 2-10 with one config; each answers the room code and host secret. |
| `POST /rooms/summary` | The multi-game dashboard's read-only poll. |
| `GET /rooms/{code}/status` | Whether a room can be joined, with the refusal reason. |
| `GET /rooms/{code}/config`, `PUT /rooms/{code}/config` | Read or save a room's config (host secret; locked once the game starts). |
| `GET /games/id/{public_id}/results`, `GET /games/id/{public_id}/export` | A stored game's results, and its CSV or JSON export; both open to anyone with the id. |
| `POST /games/claim` | Move a guest's stored results to the signed-in user. |
| `POST /users/upsert`, `GET /users/me`, `/users/me/stats`, `/users/me/games`, `/users/me/games/{public_id}` | The signed-in user's profile, stats and played games. |
| `GET /users/me/hosted-games` | The games the signed-in user hosted signed in, newest first (`games.host_user_id`; spec 13.4, 13.5). |

Shapes and error details: spec §13.4.

## Socket events

Clients send `join_waiting` (the host), `join`, `leave`, `config_update`, `start_game`,
`submit_day`, `preview_decision` (answered as the acknowledgement), `request_state` and
`end_game_early`. The server answers with the lobby, state and day events of spec §12.2, in
the order of §12.3 when a day resolves; every room event carries a `seq`, and a refusal is
`error {code, message, field?}` or, for the lobby events, `join_error`. Handlers that can fail
are wrapped in `@guarded`, so an exception never reaches the client as text.

When the last player submits, the day resolves and the next one opens. When the game ends
(the last day, or the host's early end) the room gets `game_finished` with the leaderboard,
the end-of-game stats and the full config; the game is then written to PostgreSQL in a worker
thread, outside the lock, and `game_persisted` follows once it is stored. A failed write is
retried in the background after 2 s, 10 s and 30 s, and again whenever the host or a player
rejoins or resyncs the finished room while it is not stored (spec 13.5).
