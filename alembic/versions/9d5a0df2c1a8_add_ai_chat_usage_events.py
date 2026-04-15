"""Add ai_chat_usage_event for rolling AI usage window.

Revision ID: 9d5a0df2c1a8
Revises: 8f0c2d6f6e4d
Create Date: 2025-01-21 00:00:00.000000

"""
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9d5a0df2c1a8"
down_revision: Union[str, Sequence[str], None] = "8f0c2d6f6e4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    created_table = False
    if "ai_chat_usage_event" not in existing_tables:
        op.create_table(
            "ai_chat_usage_event",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.String(length=120), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        created_table = True

    existing_indexes = (
        {idx["name"] for idx in inspector.get_indexes("ai_chat_usage_event")}
        if "ai_chat_usage_event" in existing_tables or created_table
        else set()
    )
    if "ix_ai_chat_usage_event_user_id" not in existing_indexes and (
        "ai_chat_usage_event" in existing_tables or created_table
    ):
        op.create_index("ix_ai_chat_usage_event_user_id", "ai_chat_usage_event", ["user_id"], unique=False)
    if "ix_ai_chat_usage_event_used_at" not in existing_indexes and (
        "ai_chat_usage_event" in existing_tables or created_table
    ):
        op.create_index("ix_ai_chat_usage_event_used_at", "ai_chat_usage_event", ["used_at"], unique=False)

    try:
        rows = list(bind.execute(sa.text("SELECT user_id, usage_date, used_count FROM ai_chat_usage")))
    except Exception:
        rows = []

    if rows and created_table:
        table = sa.table(
            "ai_chat_usage_event",
            sa.column("user_id", sa.String(length=120)),
            sa.column("used_at", sa.DateTime()),
        )
        inserts = []
        for user_id, usage_date, used_count in rows:
            if not usage_date:
                continue
            count = int(used_count or 0)
            if count <= 0:
                continue
            base_dt = datetime.combine(usage_date, datetime.min.time())
            inserts.extend({"user_id": user_id, "used_at": base_dt} for _ in range(count))

        if inserts:
            op.bulk_insert(table, inserts)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_chat_usage_event" not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("ai_chat_usage_event")}
    if "ix_ai_chat_usage_event_used_at" in existing_indexes:
        op.drop_index("ix_ai_chat_usage_event_used_at", table_name="ai_chat_usage_event")
    if "ix_ai_chat_usage_event_user_id" in existing_indexes:
        op.drop_index("ix_ai_chat_usage_event_user_id", table_name="ai_chat_usage_event")
    op.drop_table("ai_chat_usage_event")
