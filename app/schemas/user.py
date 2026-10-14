"""Response and request bodies of the user routes (spec 13.4)."""

from typing import Any

from pydantic import BaseModel


class UpsertRequest(BaseModel):
    """``display_name`` is sanitised by the service, whatever it holds."""

    display_name: Any = None


class UserResponse(BaseModel):
    display_name: str
    email: str | None
    photo_url: str | None


class UserStatsResponse(BaseModel):
    games_played: int
    wins: int
    total_profit: float
    best_profit: float | None
    avg_fill_rate: float | None
    avg_waste_rate: float | None
    avg_market_share: float | None


class GameListItem(BaseModel):
    game_id: str
    room_code: str
    finished_at: str
    days_played: int
    duration_days: int
    player_count: int
    ended_early: bool
    alias: str
    display_name: str
    rank: int
    total_profit: float


class GameListResponse(BaseModel):
    games: list[GameListItem]
