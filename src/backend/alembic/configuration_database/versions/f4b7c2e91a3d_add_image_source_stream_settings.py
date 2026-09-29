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
"""add image source stream settings column

Additive-only migration for stream cameras (rtsp-rtmp-stream-cameras,
design component 10): adds the nullable ``streamSettings`` JSON column to
``image_source_configuration``. An RTSP or RTMP Image_Source keeps its
transport, latency, decoder policy, maximum frame dimension, stall timeout
and Credential_Reference there; every other row stays NULL, so existing
devices upgrade in place without a data migration and no existing column
is modified. Credentials are never stored in the database.

Revision ID: f4b7c2e91a3d
Revises: e9f2a6c31b84
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4b7c2e91a3d'
down_revision: Union[str, None] = 'e9f2a6c31b84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The single column this migration adds.
COLUMN_NAME = 'streamSettings'
TABLE_NAME = 'image_source_configuration'


def _existing_columns() -> set:
    """The column names currently on ``image_source_configuration`` (empty
    when the table is somehow absent)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {col["name"] for col in inspector.get_columns(TABLE_NAME)}
    except Exception:  # noqa: BLE001 - table missing => nothing to guard against
        return set()


def upgrade() -> None:
    # Idempotent, like e9f2a6c31b84: a device that already carries the
    # column (soft deploy, state drift) survives the startup
    # ``alembic upgrade head`` instead of failing with "duplicate column".
    if COLUMN_NAME in _existing_columns():
        return
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN_NAME, sa.JSON(), nullable=True))


def downgrade() -> None:
    # Symmetric guard: only drop the column when it is actually present.
    if COLUMN_NAME not in _existing_columns():
        return
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        batch_op.drop_column(COLUMN_NAME)
