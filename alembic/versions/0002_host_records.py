"""Host records: index ``games.host_user_id``, drop ``games.host_display_name``.

``host_user_id`` is read by one query only, the profile's hosted games (spec 13.4,
13.5), so it gets its own index. ``host_display_name`` was stored and never shown.
The values already stored in ``host_user_id`` stay: they were set only for a
signed-in host, else NULL.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_games_host_user_id"), "games", ["host_user_id"], unique=False
    )
    op.drop_column("games", "host_display_name")


def downgrade() -> None:
    # The dropped names are gone; a stored game gets the default host name back.
    op.add_column(
        "games",
        sa.Column(
            "host_display_name",
            sa.String(length=24),
            server_default="Host",
            nullable=False,
        ),
    )
    op.alter_column("games", "host_display_name", server_default=None)
    op.drop_index(op.f("ix_games_host_user_id"), table_name="games")
