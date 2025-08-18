"""Create workflow_metadata table

Revision ID: be036913e5c0
Revises: ca2c5052e5b2
Create Date: 2023-11-28 13:37:53.352178

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
revision: str = 'be036913e5c0'
down_revision: Union[str, None] = 'ca2c5052e5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('workflow_metadata',
    sa.Column('workflowId', sa.VARCHAR(), nullable=False),
    sa.Column('summaryStartTime', sa.INTEGER(), nullable=False),
    sa.PrimaryKeyConstraint('workflowId')
    )
    op.create_index('ix_workflow_metadata_workflowId', 'workflow_metadata', ['workflowId'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_workflow_metadata_workflowId', table_name='workflow_metadata')
    op.drop_table('workflow_metadata')