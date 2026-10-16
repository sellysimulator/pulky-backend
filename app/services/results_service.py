"""A stored game as the ``game_finished`` summary (spec 13.4, 8.6)."""

from datetime import UTC, datetime

from app.core.stats import build_stats
from app.services.db_service import StoredGame

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def iso_utc(value: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ``; a naive datetime is taken to be UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(_TIME_FORMAT)


def results_response(game: StoredGame) -> dict:
    """The stored game's metadata plus the live ``game_finished`` stats, no ``seq``."""
    starting = float(game.config["reputation"]["starting"])
    stats = build_stats(
        records=game.records,
        market=game.market,
        settlements=game.settlements,
        display_names=game.display_names,
        starting_reputation=starting,
        seed=game.seed,
    )
    return {
        "game_id": game.game_id,
        "room_code": game.room_code,
        "host_display_name": game.host_display_name,
        "duration_days": game.duration_days,
        "days_played": game.days_played,
        "ended_early": game.ended_early,
        "started_at": iso_utc(game.started_at),
        "finished_at": iso_utc(game.finished_at),
        "leaderboard": stats["leaderboard"],
        "stats": {"players": stats["players"], "market": stats["market"]},
    }
