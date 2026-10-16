"""Profile routes: upsert, me, stats, games list and detail (spec 13.4)."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import deps
from app.db.session import get_db
from app.models.user import User
from app.schemas.results import MyResultsResponse
from app.schemas.user import (
    GameListResponse,
    UpsertRequest,
    UserResponse,
    UserStatsResponse,
)
from app.services import db_service, results_service, user_service

router = APIRouter()

NOT_FOUND = "Results not found."


def _uid(claims: dict[str, Any]) -> str:
    uid = user_service.firebase_uid(claims)
    if uid is None:
        raise HTTPException(status_code=401, detail=deps.SIGN_IN_REQUIRED)
    return uid


def _profile(user: User) -> dict[str, Any]:
    return {
        "display_name": user.display_name,
        "email": user.email,
        "photo_url": user.photo_url,
    }


@router.post("/users/upsert", response_model=UserResponse)
def upsert(
    body: UpsertRequest | None = None,
    claims: dict[str, Any] = Depends(deps.get_current_firebase_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _uid(claims)
    name = body.display_name if body is not None else None
    return _profile(user_service.upsert_user(db, claims, name))


@router.get("/users/me", response_model=UserResponse)
def me(
    claims: dict[str, Any] = Depends(deps.get_current_firebase_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _profile(user_service.ensure_user(db, claims, _uid(claims)))


@router.get("/users/me/stats", response_model=UserStatsResponse)
def my_stats(
    claims: dict[str, Any] = Depends(deps.get_current_firebase_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = user_service.find_user(db, _uid(claims))
    return user_service.user_stats(db, user)


@router.get("/users/me/games", response_model=GameListResponse)
def my_games(
    claims: dict[str, Any] = Depends(deps.get_current_firebase_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = user_service.find_user(db, _uid(claims))
    return {"games": user_service.list_user_games(db, user)}


@router.get("/users/me/games/{public_id}", response_model=MyResultsResponse)
def my_game(
    public_id: str,
    claims: dict[str, Any] = Depends(deps.get_current_firebase_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = user_service.find_user(db, _uid(claims))
    alias = user_service.alias_in_game(db, user, public_id)
    game = db_service.find_game_by_public_id(db, public_id) if alias else None
    if alias is None or game is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    stored = db_service.load_stored_game(db, game.id)
    return {**results_service.results_response(stored), "my_alias": alias}
