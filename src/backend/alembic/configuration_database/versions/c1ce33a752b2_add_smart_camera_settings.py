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
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""add smart camera settings

Revision ID: c1ce33a752b2
Revises: 25e57085e48f
Create Date: 2024-09-03 22:10:03.083941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
from utils.constants import DB_TEXT_NOTE_MAX_LENGTH

# revision identifiers, used by Alembic.
revision: str = 'c1ce33a752b2'
down_revision: Union[str, None] = '25e57085e48f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('image_source_configuration', schema=None) as batch_op:
        batch_op.add_column(sa.Column("device", sa.VARCHAR(length = DB_TEXT_NOTE_MAX_LENGTH), nullable=True))
        batch_op.add_column(sa.Column("deviceName", sa.VARCHAR(length = DB_TEXT_NOTE_MAX_LENGTH), nullable=True))
        # Since both columns are nullable, no need to set default values for backfilling or anything.


def downgrade() -> None:
    with op.batch_alter_table('image_source_configuration', schema=None) as batch_op:
        batch_op.drop_column('device')
        batch_op.drop_column('deviceName')