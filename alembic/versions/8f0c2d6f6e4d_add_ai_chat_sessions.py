"""add ai chat sessions and messages tables

Revision ID: 8f0c2d6f6e4d
Revises: 6e7c1c537f7e
Create Date: 2025-12-28 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f0c2d6f6e4d'
down_revision: Union[str, Sequence[str], None] = '6e7c1c537f7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "ai_chat_session" not in existing_tables:
        op.create_table(
            'ai_chat_session',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('session_uuid', sa.String(length=36), nullable=False),
            sa.Column('user_id', sa.String(length=120), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_interaction_at', sa.DateTime(), nullable=False),
            sa.Column('ended_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('session_uuid')
        )
    existing_session_indexes = {idx["name"] for idx in inspector.get_indexes("ai_chat_session")} if "ai_chat_session" in existing_tables else set()
    if "ix_ai_chat_session_user_id" not in existing_session_indexes and "ai_chat_session" in existing_tables:
        op.create_index('ix_ai_chat_session_user_id', 'ai_chat_session', ['user_id'], unique=False)

    if "ai_chat_message" not in existing_tables:
        op.create_table(
            'ai_chat_message',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('role', sa.String(length=20), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['ai_chat_session.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
    existing_message_indexes = {idx["name"] for idx in inspector.get_indexes("ai_chat_message")} if "ai_chat_message" in existing_tables else set()
    if "ix_ai_chat_message_session_id" not in existing_message_indexes and "ai_chat_message" in existing_tables:
        op.create_index('ix_ai_chat_message_session_id', 'ai_chat_message', ['session_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ai_chat_message_session_id', table_name='ai_chat_message')
    op.drop_table('ai_chat_message')
    op.drop_index('ix_ai_chat_session_user_id', table_name='ai_chat_session')
    op.drop_table('ai_chat_session')
