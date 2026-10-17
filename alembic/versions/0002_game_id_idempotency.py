"""A stored game is keyed by its public_id alone (spec 13.5).

A room code is reissued once its live room has expired, so the code and start time
no longer identify a game; the public_id it is given when it finishes does.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# MySQL names an unnamed UNIQUE key after its first column.
_ROOM_START_KEY = "room_code"


def upgrade() -> None:
    op.drop_constraint(_ROOM_START_KEY, "games", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(_ROOM_START_KEY, "games", ["room_code", "started_at"])
