"""The signed-in user's profile, stats and games (spec 13.4, 13.5)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.game import Game, Participant, UserStats
from app.models.user import User
from app.services import stats_service
from app.services.results_service import iso_utc
from app.services.room_service import sanitise_display_name

_STATS = select(
    UserStats.games_played,
    UserStats.wins,
    UserStats.total_profit,
    UserStats.best_profit,
    UserStats.avg_fill_rate,
    UserStats.avg_waste_rate,
    UserStats.avg_market_share,
)
# One joined query for the whole list, however many games the user played.
_GAMES = (
    select(
        Game.public_id.label("game_id"),
        Game.room_code,
        Game.finished_at,
        Game.days_played,
        Game.duration_days,
        Game.player_count,
        Game.ended_early,
        Participant.alias,
        Participant.display_name,
        Participant.final_rank.label("player_rank"),
        Participant.total_profit,
    )
    .select_from(Participant)
    .join(Game, Game.id == Participant.game_id)
    .order_by(Game.finished_at.desc(), Game.id.desc())
)
_MY_ALIAS = select(Participant.alias).join(Game, Game.id == Participant.game_id)


def firebase_uid(claims: dict) -> str | None:
    """The verified uid of a token's claims, never anything the request body holds."""
    for key in ("uid", "user_id", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _claim_name(claims: dict) -> str | None:
    name = claims.get("name")
    return name if isinstance(name, str) else None


def _claim_text(claims: dict, key: str) -> str | None:
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def find_user(db: Session, uid: str) -> User | None:
    return db.scalar(select(User).where(User.firebase_uid == uid))


def ensure_user(db: Session, claims: dict, uid: str) -> User:
    """The caller's row, created from the token when missing."""
    user = stats_service.get_or_create_user(
        db, uid, sanitise_display_name(_claim_name(claims), "Player")
    )
    changed = False
    email = _claim_text(claims, "email")
    photo = _claim_text(claims, "picture")
    if user.email is None and email is not None:
        user.email = email[:320]
        changed = True
    if user.photo_url is None and photo is not None:
        user.photo_url = photo[:1024]
        changed = True
    if changed:
        db.commit()
    return user


def upsert_user(db: Session, claims: dict, display_name: str | None) -> User:
    """Create or update the caller's row from the verified token."""
    uid = firebase_uid(claims)
    if uid is None:
        raise ValueError("token carries no uid")
    user = stats_service.get_or_create_user(db, uid)
    raw: Any = display_name if display_name is not None else _claim_name(claims)
    user.display_name = sanitise_display_name(raw, user.display_name or "Player")
    user.email = _claim_text(claims, "email")
    user.photo_url = _claim_text(claims, "picture")
    if user.email is not None:
        user.email = user.email[:320]
    if user.photo_url is not None:
        user.photo_url = user.photo_url[:1024]
    db.commit()
    return user


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def user_stats(db: Session, user: User | None) -> dict:
    row = None
    if user is not None:
        row = db.execute(_STATS.where(UserStats.user_id == user.id)).mappings().first()
    if row is None:
        return {
            "games_played": 0,
            "wins": 0,
            "total_profit": 0.0,
            "best_profit": None,
            "avg_fill_rate": None,
            "avg_waste_rate": None,
            "avg_market_share": None,
        }
    return {
        "games_played": int(row["games_played"]),
        "wins": int(row["wins"]),
        "total_profit": float(row["total_profit"]),
        "best_profit": _optional_float(row["best_profit"]),
        "avg_fill_rate": _optional_float(row["avg_fill_rate"]),
        "avg_waste_rate": _optional_float(row["avg_waste_rate"]),
        "avg_market_share": _optional_float(row["avg_market_share"]),
    }


def list_user_games(db: Session, user: User | None) -> list[dict]:
    if user is None:
        return []
    rows = db.execute(_GAMES.where(Participant.user_id == user.id)).mappings().all()
    return [
        {
            "game_id": row["game_id"],
            "room_code": row["room_code"],
            "finished_at": iso_utc(row["finished_at"]),
            "days_played": int(row["days_played"]),
            "duration_days": int(row["duration_days"]),
            "player_count": int(row["player_count"]),
            "ended_early": bool(row["ended_early"]),
            "alias": row["alias"],
            "display_name": row["display_name"],
            "rank": int(row["player_rank"]),
            "total_profit": float(row["total_profit"]),
        }
        for row in rows
    ]


def alias_in_game(db: Session, user: User | None, public_id: str) -> str | None:
    """The caller's alias in that game, ``None`` when they did not play it."""
    if user is None:
        return None
    row = db.execute(
        _MY_ALIAS.where(Participant.user_id == user.id, Game.public_id == public_id)
    ).first()
    return None if row is None else str(row[0])
