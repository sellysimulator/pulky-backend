"""Results, export and claim routes (spec 13.4, 13.8)."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
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
    user_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_FOUND = "Results not found."
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


@router.get("/games/id/{public_id}/export", response_model=None)
def export_game(
    public_id: str,
    format: Literal["csv", "json"] = Query(...),
    db: Session = Depends(get_db),
) -> Response:
    """Open to anyone who can open the game's results, like ``results_by_id``: a host
    may be a guest with no account to prove it by once the room is gone. The files
    carry only what the results show, every pulquería by its alias (no display name,
    uid, email, user id, guest identity, session token or host secret). The per-IP
    limit in ``app.core.abuse`` (``EXPORT_ROUTE``) guards the compute and counts every
    request; a file already built comes from ``export_service``'s cache, with no
    database read."""
    file = export_service.cached_file(public_id, format)
    if file is None:
        stored = _stored(db, db_service.find_game_by_public_id(db, public_id))
        file = export_service.build_file(stored, format)
        export_service.remember_file(public_id, format, file)
    return Response(
        content=file.content,
        media_type=file.media_type,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )
