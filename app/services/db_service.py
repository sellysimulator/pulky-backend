"""End-of-game persistence and stored-game loading (spec 13.5, 13.12)."""

import dataclasses
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import TextClause

from app.core.config_models import GameConfig
from app.core.engine import PulkyEngine
from app.core.enums import RoomState
from app.core.money import round_cents
from app.core.records import DayRecord, FinalSettlement, MarketDay
from app.db import session as db_session_module
from app.models.game import Game
from app.services import stats_service

logger = logging.getLogger(__name__)

CONFIG_SCHEMA_VERSION = 1
GUEST_PREFIX = "guest_"
_ROOM_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_PLAYER_DAY_FIELDS = tuple(f.name for f in dataclasses.fields(DayRecord))
_MARKET_DAY_FIELDS = tuple(f.name for f in dataclasses.fields(MarketDay))
_PARTICIPANT_STATS = (
    "total_profit",
    "units_sold",
    "units_spoiled",
    "units_lost_transport",
    "overall_market_share",
    "fill_rate",
    "waste_rate",
    "avg_reputation",
    "final_reputation",
    "futures_units",
    "cost_insurance",
    "refund_insurance",
    "peer_units_bought",
    "peer_units_sold",
    "cost_peer",
    "revenue_peer",
)
_SETTLEMENT_FIELDS = (
    "liquidated_units",
    "revenue_liquidation",
    "refund_insurance_in_transit",
    "futures_cancelled_units",
    "refund_futures_cancelled",
)
_GAME_FIELDS = (
    "public_id",
    "room_code",
    "host_user_id",
    "host_display_name",
    "rng_seed",
    "duration_days",
    "days_played",
    "ended_early",
    "player_count",
    "started_at",
    "finished_at",
)
_PARTICIPANT_FIELDS = (
    "game_id",
    "alias",
    "display_name",
    "user_id",
    "guest_identity",
    "final_rank",
    *_PARTICIPANT_STATS,
    *_SETTLEMENT_FIELDS,
)
_PLAYER_DAY_COLUMNS = ("game_id", "alias", *_PLAYER_DAY_FIELDS)
_MARKET_DAY_COLUMNS = ("game_id", *_MARKET_DAY_FIELDS)


def _insert(table: str, columns: tuple[str, ...]) -> TextClause:
    names = ", ".join(columns)
    binds = ", ".join(f":{c}" for c in columns)
    return text(f"INSERT INTO {table} ({names}) VALUES ({binds})")


_INSERT_GAME = _insert("games", _GAME_FIELDS)
_INSERT_CONFIG = _insert("game_configs", ("game_id", "config_schema_version", "config"))
_INSERT_PARTICIPANT = _insert("participants", _PARTICIPANT_FIELDS)
_INSERT_PLAYER_DAY = _insert("player_days", _PLAYER_DAY_COLUMNS)
_INSERT_MARKET_DAY = _insert("market_days", _MARKET_DAY_COLUMNS)


class _Redacted(Exception):
    """Stands in for a driver error in logs: those carry host, SQL and bound values."""


def _log_failure(message: str, room_code: str, error: BaseException) -> None:
    redacted = _Redacted(type(error).__name__)
    logger.error(
        message,
        room_code,
        exc_info=(_Redacted, redacted, error.__traceback__),
    )


def _parse_room_time(value: str) -> datetime:
    return datetime.strptime(value, _ROOM_TIME_FORMAT)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_DOUBLE_COLUMNS = frozenset(
    {
        "fill",
        "waste",
        "market_share",
        "reputation_open",
        "reputation_end",
        "weekday_multiplier",
        "expected_demand",
        "overall_market_share",
        "fill_rate",
        "waste_rate",
        "avg_reputation",
        "final_reputation",
    }
)


def _exact(row: dict[str, Any]) -> dict[str, Any]:
    """PyMySQL writes floats with 15 significant digits, which loses the last bits of
    a DOUBLE; the shortest round-trip text is parsed exactly by MySQL."""
    return {
        k: repr(v) if k in _DOUBLE_COLUMNS and isinstance(v, float) else v
        for k, v in row.items()
    }


def _is_guest(identity: str) -> bool:
    return identity.startswith(GUEST_PREFIX)


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _find_by_key(db: Session, room_code: str, started_at: datetime) -> Game | None:
    return db.scalar(
        select(Game)
        .where(Game.room_code == room_code, Game.started_at == started_at)
        .execution_options(populate_existing=True)
    )


def _default_session() -> Session:
    factory = db_session_module.SessionLocal
    if isinstance(factory, sessionmaker) and factory.kw.get("bind") is None:
        return factory(bind=db_session_module.get_engine())
    return factory()


def _resolve_identity(
    db: Session, identity: str, display_name: str
) -> tuple[int | None, str | None]:
    """(user id, guest identity): exactly one of the two is set."""
    if _is_guest(identity):
        return None, identity
    user = stats_service.get_or_create_user(db, identity, display_name)
    return user.id, None


def _write_game(db: Session, room: dict, engine: PulkyEngine) -> tuple[str, list[int]]:
    code = room["room_code"]
    standings = engine.standings()
    settlements = engine.settlements()
    market = engine.market_history()
    aliases = list(engine.aliases)
    room_players: Mapping[str, Mapping[Any, Any]] = room["participants"]

    host_identity = room.get("host_identity")
    host_user_id: int | None = None
    if host_identity and not _is_guest(host_identity):
        host_user_id, _ = _resolve_identity(
            db, host_identity, room["host_display_name"]
        )

    resolved: dict[str, tuple[int | None, str | None]] = {}
    for alias in aliases:
        resolved[alias] = _resolve_identity(
            db, room_players[alias]["identity"], engine.display_names[alias]
        )

    public_id = uuid.uuid4().hex
    result = db.execute(
        _INSERT_GAME,
        {
            "public_id": public_id,
            "room_code": code,
            "host_user_id": host_user_id,
            "host_display_name": room["host_display_name"],
            "rng_seed": room["seed"],
            "duration_days": engine.config.duration_days,
            "days_played": len(market),
            "ended_early": engine.ended_early,
            "player_count": len(aliases),
            "started_at": _parse_room_time(room["started_at"]),
            "finished_at": _parse_room_time(room["finished_at"]),
        },
    )
    game_id = cast(CursorResult[Any], result).lastrowid

    db.execute(
        _INSERT_CONFIG,
        {
            "game_id": game_id,
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "config": _dumps(room["config"]),
        },
    )

    participant_rows = []
    for alias in aliases:
        stats = standings["players"][alias]
        settlement = settlements[alias].to_payload()
        user_id, guest_identity = resolved[alias]
        row: dict[str, Any] = {
            "game_id": game_id,
            "alias": alias,
            "display_name": engine.display_names[alias],
            "user_id": user_id,
            "guest_identity": guest_identity,
            "final_rank": stats["rank"],
        }
        row.update({k: stats[k] for k in _PARTICIPANT_STATS})
        row.update({k: settlement[k] for k in _SETTLEMENT_FIELDS})
        participant_rows.append(row)
    db.execute(_INSERT_PARTICIPANT, [_exact(r) for r in participant_rows])

    day_rows = []
    for alias in aliases:
        for record in engine.records(alias):
            payload = record.to_payload()
            payload["game_id"] = game_id
            payload["alias"] = alias
            payload["conserve_lots"] = _dumps(payload["conserve_lots"])
            payload["peer_trades"] = _dumps(payload["peer_trades"])
            for key in ("peer_buy", "peer_sell"):
                payload[key] = None if payload[key] is None else _dumps(payload[key])
            day_rows.append(_exact({k: payload[k] for k in _PLAYER_DAY_COLUMNS}))
    if day_rows:
        db.execute(_INSERT_PLAYER_DAY, day_rows)

    market_rows = []
    for day in market:
        payload = day.to_payload()
        payload["game_id"] = game_id
        market_rows.append(_exact({k: payload[k] for k in _MARKET_DAY_COLUMNS}))
    if market_rows:
        db.execute(_INSERT_MARKET_DAY, market_rows)

    user_ids = sorted({uid for uid, _ in resolved.values() if uid is not None})
    return public_id, user_ids


def persist_finished_game(
    room: dict, session_factory: Callable[[], Session] | None = None
) -> str | None:
    """Write a finished room to MySQL once; the game's public_id, or None on failure."""
    code = str(room.get("room_code"))
    if room.get("state") != RoomState.FINISHED or not room.get("engine"):
        logger.error("room %s cannot be persisted: it is not finished", code)
        return None

    db: Session | None = None
    try:
        engine = PulkyEngine.from_payload(
            room["engine"], GameConfig.from_dict(room["config"])
        )
        started_at = _parse_room_time(room["started_at"])
        db = session_factory() if session_factory is not None else _default_session()

        existing = _find_by_key(db, code, started_at)
        if existing is not None:
            return existing.public_id

        try:
            public_id, user_ids = _write_game(db, room, engine)
            db.commit()
        except IntegrityError:
            db.rollback()
            found = _find_by_key(db, code, started_at)
            if found is None:
                raise
            return found.public_id

        logger.info("room %s persisted as %s", code, public_id)
        try:
            stats_service.rebuild_user_stats(db, user_ids)
        except Exception as error:
            db.rollback()
            _log_failure("room %s stats rebuild failed", code, error)
        return public_id
    except Exception as error:
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: S110  # nothing more to undo
                pass
        _log_failure("room %s could not be persisted", code, error)
        return None
    finally:
        if db is not None:
            db.close()


@dataclass(frozen=True)
class StoredGame:
    game_id: str
    room_code: str
    host_display_name: str
    duration_days: int
    days_played: int
    ended_early: bool
    started_at: datetime
    finished_at: datetime
    seed: int
    config: dict
    records: dict[str, list[DayRecord]]
    market: list[MarketDay]
    settlements: dict[str, FinalSettlement]
    display_names: dict[str, str]
    user_ids: dict[str, int | None]
    guest_identities: dict[str, str | None]


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


_BOOL_FIELDS = frozenset(
    {"cooperate", "refrigeration_failure", "disrupted", "cooperation"}
)


def _plain(name: str, value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if name in _BOOL_FIELDS and value is not None:
        return bool(value)
    return value


def _row_payload(row: Mapping[Any, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _plain(name, row[name]) for name in fields}


def _day_record(row: Mapping[Any, Any]) -> DayRecord:
    data = _row_payload(row, _PLAYER_DAY_FIELDS)
    data["conserve_lots"] = list(_json_value(row["conserve_lots"]) or [])
    data["peer_trades"] = list(_json_value(row["peer_trades"]) or [])
    data["peer_buy"] = _json_value(row["peer_buy"])
    data["peer_sell"] = _json_value(row["peer_sell"])
    return DayRecord.from_payload(data)


def _market_day(row: Mapping[Any, Any]) -> MarketDay:
    return MarketDay.from_payload(_row_payload(row, _MARKET_DAY_FIELDS))


def _settlement(row: Mapping[Any, Any]) -> FinalSettlement:
    data = _row_payload(row, _SETTLEMENT_FIELDS)
    data["settlement_total"] = round_cents(
        data["revenue_liquidation"]
        + data["refund_insurance_in_transit"]
        + data["refund_futures_cancelled"]
    )
    return FinalSettlement.from_payload(data)


# Plain SELECTs, not the ORM: its DOUBLE type returns Decimal rounded to ten places,
# which would break exact parity with the live statistics.
_SELECT_PARTICIPANTS = text("SELECT * FROM participants WHERE game_id = :g ORDER BY id")
_SELECT_PLAYER_DAYS = text(
    "SELECT * FROM player_days WHERE game_id = :g ORDER BY day, id"
)
_SELECT_MARKET_DAYS = text("SELECT * FROM market_days WHERE game_id = :g ORDER BY day")
_SELECT_CONFIG = text("SELECT config FROM game_configs WHERE game_id = :g")


def load_stored_game(db: Session, game_pk: int) -> StoredGame:
    """One game read back as the exact inputs of ``build_stats``."""
    params = {"g": game_pk}
    game = db.scalars(
        select(Game).where(Game.id == game_pk).execution_options(populate_existing=True)
    ).one()
    config = db.execute(_SELECT_CONFIG, params).scalar_one()
    participants = db.execute(_SELECT_PARTICIPANTS, params).mappings().all()
    days = db.execute(_SELECT_PLAYER_DAYS, params).mappings().all()
    market_rows = db.execute(_SELECT_MARKET_DAYS, params).mappings().all()

    records: dict[str, list[DayRecord]] = {p["alias"]: [] for p in participants}
    for day_row in days:
        records[day_row["alias"]].append(_day_record(day_row))

    return StoredGame(
        game_id=game.public_id,
        room_code=game.room_code,
        host_display_name=game.host_display_name,
        duration_days=game.duration_days,
        days_played=game.days_played,
        ended_early=bool(game.ended_early),
        started_at=_as_utc(game.started_at),
        finished_at=_as_utc(game.finished_at),
        seed=int(game.rng_seed),
        config=_json_value(config),
        records=records,
        market=[_market_day(m) for m in market_rows],
        settlements={p["alias"]: _settlement(p) for p in participants},
        display_names={p["alias"]: p["display_name"] for p in participants},
        user_ids={p["alias"]: p["user_id"] for p in participants},
        guest_identities={p["alias"]: p["guest_identity"] for p in participants},
    )


def find_game_by_public_id(db: Session, public_id: str) -> Game | None:
    return db.scalar(select(Game).where(Game.public_id == public_id))


def find_latest_game_by_room_code(db: Session, room_code: str) -> Game | None:
    return db.scalar(
        select(Game)
        .where(func.upper(Game.room_code) == room_code.upper())
        .order_by(Game.finished_at.desc(), Game.id.desc())
        .limit(1)
    )


__all__ = [
    "StoredGame",
    "find_game_by_public_id",
    "find_latest_game_by_room_code",
    "load_stored_game",
    "persist_finished_game",
]
