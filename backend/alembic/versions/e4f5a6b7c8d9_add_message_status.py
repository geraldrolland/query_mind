"""add message status column

Revision ID: e4f5a6b7c8d9
Revises: 9d3b7e5a1c48
Create Date: 2026-08-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, Sequence[str], None] = '9d3b7e5a1c48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE message ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'sent'")


def downgrade() -> None:
    op.execute("ALTER TABLE message DROP COLUMN IF EXISTS status")
