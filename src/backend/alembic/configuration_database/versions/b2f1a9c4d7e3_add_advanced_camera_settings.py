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

"""add advanced camera settings

Revision ID: b2f1a9c4d7e3
Revises: c1ce33a752b2
Create Date: 2026-06-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2f1a9c4d7e3'
down_revision: Union[str, None] = 'c1ce33a752b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('image_source_configuration', schema=None) as batch_op:
        # Nullable JSON column holding the persisted advanced GenICam controls
        # (reverseX, reverseY, balanceWhiteAuto). Nullable, so existing rows
        # backfill to NULL and no data migration is required.
        batch_op.add_column(sa.Column("advancedSettings", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('image_source_configuration', schema=None) as batch_op:
        batch_op.drop_column('advancedSettings')
