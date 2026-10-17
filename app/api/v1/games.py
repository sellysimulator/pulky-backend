"""Results, export and claim routes (spec 13.4, 13.8)."""

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api import deps
from app.db.session import get_db
from app.models.game import Game
from app.schemas.results import ClaimRequest, ClaimResponse, ResultsResponse
from app.services import (
    claim_service,
    db_service,
    export_service,
    results_service,
    room_service,
    state_service,
    user_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_FOUND = "Results not found."
EXPORT_FORBIDDEN = "Only the host who created this room can export it."
NOT_A_GUEST = "That is not a guest id."


def _stored(db: Session, game: Game | None) -> db_service.StoredGame:
    if game is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return db_service.load_stored_game(db, game.id)


@router.post("/games/claim", response_model=ClaimResponse)
def claim_games(
    body: ClaimRequest,
    claims: dict[str, Any] = Depends(deps.get_current_firebase_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    guest_identity = body.guest_identity
    if not claim_service.is_guest_identity(guest_identity):
        raise HTTPException(status_code=400, detail=NOT_A_GUEST)
    uid = user_service.firebase_uid(claims)
    if uid is None:
        raise HTTPException(status_code=401, detail=deps.SIGN_IN_REQUIRED)
    return {"claimed": claim_service.claim_guest_results(db, uid, guest_identity)}


@router.get("/games/id/{public_id}/results", response_model=ResultsResponse)
def results_by_id(public_id: str, db: Session = Depends(get_db)) -> dict:
    game = db_service.find_game_by_public_id(db, public_id)
    return results_service.results_response(_stored(db, game))


async def _may_export(
    db: Session, game: Game, secret: str | None, authorization: str | None
) -> bool:
    # The secret is the live room's; the game id check keeps a later room that reuses
    # the code from authorising an export of this game.
    room = await state_service.get_state_service().get_room(game.room_code)
    if (
        room is not None
        and room.get("game_id") == game.public_id
        and room_service.check_host_secret(room, {"host_secret": secret})
    ):
        return True
    if game.host_user_id is None or not authorization:
        return False
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return False
    claims = deps.verify_firebase_id_token(parts[1])
    uid = user_service.firebase_uid(claims) if claims else None
    if uid is None:
        return False
    user = user_service.find_user(db, uid)
    return user is not None and user.id == game.host_user_id


@router.get("/games/id/{public_id}/export", response_model=None)
async def export_game(
    public_id: str,
    format: Literal["csv", "json"] = Query(...),
    x_host_secret: str | None = Header(None),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> Response:
    game = db_service.find_game_by_public_id(db, public_id)
    if game is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if not await _may_export(db, game, x_host_secret, authorization):
        raise HTTPException(status_code=403, detail=EXPORT_FORBIDDEN)
    stored = db_service.load_stored_game(db, game.id)
    name = f"pulky-{stored.room_code}-{stored.game_id[:8]}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if format == "csv":
        return Response(
            content=export_service.export_csv(stored),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )
    return Response(
        content=json.dumps(export_service.export_json(stored)),
        media_type="application/json",
        headers=headers,
    )
