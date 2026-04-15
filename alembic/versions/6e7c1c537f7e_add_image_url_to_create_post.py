"""add image url to create_post

Revision ID: 6e7c1c537f7e
Revises: b7f65e9ad5c3
Create Date: 2025-12-28 04:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e7c1c537f7e'
down_revision: Union[str, Sequence[str], None] = 'b7f65e9ad5c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'create_post',
        sa.Column('image_url', sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('create_post', 'image_url')
