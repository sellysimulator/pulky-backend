"""User accounts and aggregate player statistics (spec 13.5)."""

from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import Select, Table, case, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.limits import MAX_DISPLAY_NAME
from app.models.game import Participant, UserStats
from app.models.user import User

DEFAULT_DISPLAY_NAME = "Player"

_STAT_COLUMNS = (
    "games_played",
    "wins",
    "total_profit",
    "best_profit",
    "avg_fill_rate",
    "avg_waste_rate",
    "avg_market_share",
)


def _aggregate(ids: list[int]) -> Select[Any]:
    """One grouped query for every given user (spec 13.5: no N+1)."""
    return (
        select(
            Participant.user_id,
            func.count().label("games_played"),
            func.sum(case((Participant.final_rank == 1, 1), else_=0)).label("wins"),
            func.sum(Participant.total_profit).label("total_profit"),
            func.max(Participant.total_profit).label("best_profit"),
            func.avg(Participant.fill_rate).label("avg_fill_rate"),
            func.avg(Participant.waste_rate).label("avg_waste_rate"),
            func.avg(Participant.overall_market_share).label("avg_market_share"),
        )
        .where(Participant.user_id.in_(ids))
        .group_by(Participant.user_id)
    )


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def rebuild_user_stats(db: Session, user_ids: Iterable[int]) -> None:
    """Recompute and upsert ``user_stats`` for the given users, then commit."""
    ids = sorted(set(user_ids))
    if not ids:
        return
    rows = db.execute(_aggregate(ids)).mappings().all()
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
        # MySQL's upsert: a concurrent rebuild for the same user is last-writer-wins.
        # On the Table, not the entity: the ORM bulk path drops None values per row.
        upsert = mysql_insert(cast(Table, UserStats.__table__))
        upsert = upsert.on_duplicate_key_update(
            {name: upsert.inserted[name] for name in _STAT_COLUMNS}
        )
        db.execute(upsert, params)
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
