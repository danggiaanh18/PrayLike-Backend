"""Drop obsolete ai_chat_usage table.

Revision ID: 1c3c62f5d3af
Revises: 9d5a0df2c1a8
Create Date: 2025-01-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1c3c62f5d3af"
down_revision: Union[str, Sequence[str], None] = "9d5a0df2c1a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop ai_chat_usage if it still exists."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_chat_usage" not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("ai_chat_usage")}
    for idx_name in ("ix_ai_chat_usage_usage_date", "ix_ai_chat_usage_user_id"):
        if idx_name in existing_indexes:
            op.drop_index(idx_name, table_name="ai_chat_usage")
    op.drop_table("ai_chat_usage")


def downgrade() -> None:
    """Recreate ai_chat_usage table for rollback."""
    op.create_table(
        "ai_chat_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=120), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "usage_date", name="uq_ai_chat_usage_user_date"),
    )
    op.create_index("ix_ai_chat_usage_usage_date", "ai_chat_usage", ["usage_date"], unique=False)
    op.create_index("ix_ai_chat_usage_user_id", "ai_chat_usage", ["user_id"], unique=False)
