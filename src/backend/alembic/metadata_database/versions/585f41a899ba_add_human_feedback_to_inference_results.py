"""add human feedback to inference results

Revision ID: 585f41a899ba
Revises: 3189912293ea
Create Date: 2024-04-02 22:47:37.250805

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

from utils.constants import ANOMALY, DB_TEXT_NOTE_MAX_LENGTH, NORMAL


# revision identifiers, used by Alembic.
revision: str = '585f41a899ba'
down_revision: Union[str, None] = '3189912293ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('inference_result_metadata', schema=None) as batch_op:
        batch_op.add_column(sa.Column("humanClassification", sa.Enum(ANOMALY, NORMAL, name="enum_prediction_type"), nullable=True))
        batch_op.add_column(sa.Column("textNote", sa.VARCHAR(length = DB_TEXT_NOTE_MAX_LENGTH), nullable=True))
        # Since both columns are nullable, no need to set default values for backfilling or anything.

def downgrade() -> None:
    with op.batch_alter_table('inference_result_metadata', schema=None) as batch_op:
        batch_op.drop_column('humanClassification')
        batch_op.drop_column('textNote')
