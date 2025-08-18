"""create latency time table

Revision ID: 70c7b3e7f6e0
Revises: be036913e5c0
Create Date: 2024-01-09 21:15:58.284338

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
revision: str = '70c7b3e7f6e0'
down_revision: Union[str, None] = 'be036913e5c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None: 
    op.create_table('latency_time',
    sa.Column('inferenceCaptureId', sa.VARCHAR(), nullable=False),
    sa.Column('latencyType', sa.VARCHAR(), nullable=False), 
    sa.Column('timestamp', sa.FLOAT(), nullable=False),
    sa.PrimaryKeyConstraint('inferenceCaptureId', 'latencyType')
    )
    op.create_index('ix_latency_time_inferenceCaptureId', 'latency_time', ['inferenceCaptureId'], unique=False)

def downgrade() -> None:
    op.drop_index('ix_latency_time_inferenceCaptureId', table_name='latency_time')
    op.drop_table('latency_time')