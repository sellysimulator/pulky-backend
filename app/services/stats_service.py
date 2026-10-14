"""User accounts and aggregate player statistics (spec 13.5)."""

from collections.abc import Iterable

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.limits import MAX_DISPLAY_NAME
from app.models.user import User

DEFAULT_DISPLAY_NAME = "Player"

_AGGREGATE = text(
    "SELECT user_id, COUNT(*) AS games_played, "
    "SUM(CASE WHEN final_rank = 1 THEN 1 ELSE 0 END) AS wins, "
    "SUM(total_profit) AS total_profit, MAX(total_profit) AS best_profit, "
    "AVG(fill_rate) AS avg_fill_rate, AVG(waste_rate) AS avg_waste_rate, "
    "AVG(overall_market_share) AS avg_market_share "
    "FROM participants WHERE user_id IN :ids GROUP BY user_id"
).bindparams(bindparam("ids", expanding=True))

_UPSERT = text(
    "INSERT INTO user_stats (user_id, games_played, wins, total_profit, "
    "best_profit, avg_fill_rate, avg_waste_rate, avg_market_share) "
    "VALUES (:user_id, :games_played, :wins, :total_profit, :best_profit, "
    ":avg_fill_rate, :avg_waste_rate, :avg_market_share) "
    "ON DUPLICATE KEY UPDATE games_played = VALUES(games_played), "
    "wins = VALUES(wins), total_profit = VALUES(total_profit), "
    "best_profit = VALUES(best_profit), avg_fill_rate = VALUES(avg_fill_rate), "
    "avg_waste_rate = VALUES(avg_waste_rate), "
    "avg_market_share = VALUES(avg_market_share)"
)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def rebuild_user_stats(db: Session, user_ids: Iterable[int]) -> None:
    """Recompute and upsert ``user_stats`` for the given users, then commit."""
    ids = sorted(set(user_ids))
    if not ids:
        return
    rows = db.execute(_AGGREGATE, {"ids": ids}).mappings().all()
    params = [
        {
            "user_id": int(row["user_id"]),
            "games_played": int(row["games_played"]),
            "wins": int(row["wins"]),
            "total_profit": float(row["total_profit"]),
            "best_profit": float(row["best_profit"]),
            "avg_fill_rate": _optional_float(row["avg_fill_rate"]),
            "avg_waste_rate": _optional_float(row["avg_waste_rate"]),
            "avg_market_share": _optional_float(row["avg_market_share"]),
        }
        for row in rows
    ]
    if params:
        db.execute(_UPSERT, params)
    db.commit()


def get_or_create_user(
    db: Session, firebase_uid: str, display_name: str | None = None
) -> User:
    """The user with this Firebase uid, created and committed at once if absent.

    Committing immediately keeps account creation out of any longer transaction, so
    concurrent writers never wait on each other's uncommitted user rows.
    """
    existing = db.scalar(select(User).where(User.firebase_uid == firebase_uid))
    if existing is not None:
        return existing
    name = (display_name or DEFAULT_DISPLAY_NAME)[:MAX_DISPLAY_NAME]
    user = User(
        firebase_uid=firebase_uid, display_name=name, email=None, photo_url=None
    )
    try:
        db.add(user)
        db.commit()
    except IntegrityError:
        # A concurrent writer created it first; a fresh transaction sees its commit.
        db.rollback()
        found = db.scalar(select(User).where(User.firebase_uid == firebase_uid))
        if found is None:
            raise
        return found
    return user
