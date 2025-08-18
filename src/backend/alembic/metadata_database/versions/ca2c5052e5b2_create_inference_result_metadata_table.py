"""create inference result metadata table

Revision ID: ca2c5052e5b2
Revises: 
Create Date: 2023-11-22 09:55:49.732945

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

from utils.constants import ANOMALY, NORMAL

# revision identifiers, used by Alembic.
revision: str = 'ca2c5052e5b2'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table('inference_result_metadata',
    sa.Column('captureId', sa.VARCHAR(), nullable=False),
    sa.Column('workflowId', sa.VARCHAR(), nullable=True),
    sa.Column('inferenceCreationTime', sa.INTEGER(), nullable=True),
    sa.Column('prediction', sa.Enum(ANOMALY, NORMAL, name="enum_prediction_type"), nullable=True),
    sa.Column('confidence', sa.FLOAT(), nullable=True),
    sa.Column('anomalyLabels', sqlite.JSON(), nullable=True),
    sa.Column('anomalyScore', sa.FLOAT(), nullable=True),
    sa.Column('anomalyThreshod', sa.FLOAT(), nullable=True),
    sa.Column('maskImage', sa.VARCHAR(), nullable=True),
    sa.Column('maskBackground', sqlite.JSON(), nullable=True),
    sa.Column('inputImageFilePath', sa.VARCHAR(), nullable=True),
    sa.Column('outputImageFilePath', sa.VARCHAR(), nullable=True),
    sa.Column('modelId', sa.VARCHAR(), nullable=True),
    sa.Column('modelName', sa.VARCHAR(), nullable=True),
    sa.Column('flagForReview', sa.BOOLEAN(), nullable=True),
    sa.PrimaryKeyConstraint('captureId')
    )
    op.create_index('ix_inference_result_metadata_workflowId', 'inference_result_metadata', ['workflowId'], unique=False)
    op.create_index('ix_inference_result_metadata_captureId', 'inference_result_metadata', ['captureId'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_inference_result_metadata_workflowId', table_name='inference_result_metadata')
    op.drop_index('ix_inference_result_metadata_captureId', table_name='inference_result_metadata')
    op.drop_table('inference_result_metadata')