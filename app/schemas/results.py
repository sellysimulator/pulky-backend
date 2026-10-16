"""Response and request bodies of the results, export and claim routes (spec 13.4)."""

from typing import Any

from pydantic import BaseModel


class ResultsResponse(BaseModel):
    game_id: str
    room_code: str
    host_display_name: str
    duration_days: int
    days_played: int
    ended_early: bool
    started_at: str
    finished_at: str
    leaderboard: list[dict[str, Any]]
    stats: dict[str, Any]


class MyResultsResponse(ResultsResponse):
    my_alias: str


class ClaimRequest(BaseModel):
    """``guest_identity`` is validated by the service, whatever it holds."""

    guest_identity: Any = None


class ClaimResponse(BaseModel):
    claimed: int
