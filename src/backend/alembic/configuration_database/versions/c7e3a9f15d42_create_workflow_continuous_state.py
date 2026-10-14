#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""create workflow continuous state table

Additive-only migration for continuous stream workflows
(rtsp-rtmp-stream-cameras, design component 14): one row per continuous
registration holding the operator pause (which survives restarts until
the operator resumes) and the latest counter snapshot. A device without
continuous workflows never writes a row, and no existing table is
touched.

Revision ID: c7e3a9f15d42
Revises: b8d4f0a26c57
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e3a9f15d42'
down_revision: Union[str, None] = 'b8d4f0a26c57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'workflow_continuous_state'


def _table_exists() -> bool:
    return TABLE_NAME in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # Idempotent, like the other stream-camera migrations: a device that
    # already has the table survives the startup ``alembic upgrade head``.
    if _table_exists():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column('registration_id', sa.VARCHAR(), nullable=False),
        sa.Column('paused', sa.BOOLEAN(), nullable=False),
        sa.Column('paused_at', sa.INTEGER(), nullable=True),
        sa.Column('counters_json', sa.TEXT(), nullable=True),
        sa.Column('updated_at', sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint('registration_id'),
    )


def downgrade() -> None:
    if not _table_exists():
        return
    op.drop_table(TABLE_NAME)
