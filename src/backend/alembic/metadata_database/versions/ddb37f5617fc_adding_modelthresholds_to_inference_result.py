"""Adding ModelConfidenceThresholds to InferenceResults

Revision ID: ddb37f5617fc
Revises: 585f41a899ba
Create Date: 2024-04-25 13:49:04.242206

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
from sqlalchemy.dialects import sqlite


# revision identifiers, used by Alembic.
revision: str = 'ddb37f5617fc'
down_revision: Union[str, None] = '585f41a899ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('inference_result_metadata', schema=None) as batch_op:
        batch_op.add_column(sa.Column("humanReviewRequired", sa.BOOLEAN(), nullable=True))
        batch_op.add_column(sa.Column("modelConfidenceThresholds", sqlite.JSON(), nullable=True))
        # Since both columns are nullable, no need to set default values for backfilling or anything.


def downgrade() -> None:
    with op.batch_alter_table('inference_result_metadata', schema=None) as batch_op:
        batch_op.drop_column('humanReviewRequired')
        batch_op.drop_column('modelConfidenceThresholds')
