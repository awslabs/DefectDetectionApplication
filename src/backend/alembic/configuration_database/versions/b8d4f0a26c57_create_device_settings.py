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
"""create device settings table

Additive-only migration for stream cameras (rtsp-rtmp-stream-cameras,
design component 11, ``settings.py``): a key/value table for device-level
limits (the stream session limit, the continuous-run retention and staging
byte caps). An absent row means the default, so an upgraded device behaves
exactly as before until a limit is set. No existing table is touched.

Revision ID: b8d4f0a26c57
Revises: f4b7c2e91a3d
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d4f0a26c57'
down_revision: Union[str, None] = 'f4b7c2e91a3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'device_settings'


def _table_exists() -> bool:
    return TABLE_NAME in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # Idempotent, like the other stream-camera migration: a device that
    # already has the table survives the startup ``alembic upgrade head``.
    if _table_exists():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column('key', sa.VARCHAR(), nullable=False),
        sa.Column('value', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    if not _table_exists():
        return
    op.drop_table(TABLE_NAME)
