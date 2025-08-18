"""Add capture type to inference result metadata

Revision ID: 37b6e36641e7
Revises: ddb37f5617fc
Create Date: 2024-05-27 02:18:48.539953

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

from utils.constants import CAPTURE, INFERENCE

# revision identifiers, used by Alembic.
revision: str = '37b6e36641e7'
down_revision: Union[str, None] = 'ddb37f5617fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inference_result_metadata", sa.Column("captureType", sa.Enum(CAPTURE, INFERENCE, name="enum_capture_type")))
    # Backfill existing entries with default value
    op.execute("UPDATE inference_result_metadata SET captureType = 'Inference'")


def downgrade() -> None:
    with op.batch_alter_table('inference_result_metadata', schema=None) as batch_op:
        batch_op.drop_column('captureType')
    