"""Attach a guest's stored results to their account (spec 13.4, 13.8)."""

import re

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.game import Participant
from app.services import stats_service

GUEST_ID_PATTERN = re.compile(r"^guest_[0-9a-f-]{36}$")


def is_guest_identity(value: object) -> bool:
    return isinstance(value, str) and GUEST_ID_PATTERN.fullmatch(value) is not None


def claim_guest_results(db: Session, firebase_uid: str, guest_identity: str) -> int:
    """Move every unclaimed row of ``guest_identity`` to the caller; the row count."""
    user = stats_service.get_or_create_user(db, firebase_uid)
    result = db.execute(
        update(Participant)
        .where(
            Participant.guest_identity == guest_identity,
            Participant.user_id.is_(None),
        )
        .values(user_id=user.id, guest_identity=None)
    )
    claimed = int(getattr(result, "rowcount", 0) or 0)
    db.commit()
    stats_service.rebuild_user_stats(db, [user.id])
    return claimed
