"""add avatar url to user profile

Revision ID: b7f65e9ad5c3
Revises: e0c7a8abd8cd
Create Date: 2025-12-28 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f65e9ad5c3'
down_revision: Union[str, Sequence[str], None] = 'e0c7a8abd8cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'user_profile',
        sa.Column('avatar_url', sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_profile', 'avatar_url')
