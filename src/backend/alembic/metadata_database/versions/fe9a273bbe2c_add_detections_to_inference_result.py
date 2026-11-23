"""Add detections (Detection_List, list form) to inference result metadata

Revision ID: fe9a273bbe2c
Revises: 37b6e36641e7
Create Date: 2026-11-23 06:23:19.000000

"""
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
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fe9a273bbe2c'
down_revision: Union[str, None] = '37b6e36641e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # rfdetr-seg-support Requirement 10.5b (DD13): the Detection_List of an
    # instance-segmentation (or detection) capture in list form. Nullable, so
    # existing rows need no backfill and classification / anomaly rows stay NULL.
    op.add_column('inference_result_metadata', sa.Column('detections', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('inference_result_metadata', schema=None) as batch_op:
        batch_op.drop_column('detections')
