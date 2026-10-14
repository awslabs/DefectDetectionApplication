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

"""create workflow engine tables

Additive-only migration for the Workflow Manager feature: creates the
workflow_registrations and workflow_executions tables used by the
LocalServer workflow engine (workflow_engine package). No existing
table is modified (Requirement 13.5).

Revision ID: a4f8c2d91e57
Revises: b2f1a9c4d7e3
Create Date: 2026-06-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a4f8c2d91e57'
down_revision: Union[str, None] = 'b2f1a9c4d7e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workflow_registrations',
        sa.Column('id', sa.VARCHAR(), nullable=False),
        sa.Column('workflow_id', sa.VARCHAR(), nullable=False),
        sa.Column('version', sa.VARCHAR(), nullable=False),
        sa.Column('arch', sa.VARCHAR(), nullable=False),
        sa.Column('artifact_path', sa.VARCHAR(), nullable=False),
        sa.Column('status', sa.VARCHAR(), nullable=False),
        sa.Column('registered_at', sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_workflow_registrations_id', 'workflow_registrations', ['id'], unique=False
    )
    op.create_index(
        'ix_workflow_registrations_workflow_id',
        'workflow_registrations',
        ['workflow_id'],
        unique=False,
    )

    op.create_table(
        'workflow_executions',
        sa.Column('id', sa.VARCHAR(), nullable=False),
        sa.Column('registration_id', sa.VARCHAR(), nullable=False),
        sa.Column('started_at', sa.INTEGER(), nullable=True),
        sa.Column('finished_at', sa.INTEGER(), nullable=True),
        sa.Column('status', sa.VARCHAR(), nullable=False),
        sa.Column('failing_node_id', sa.VARCHAR(), nullable=True),
        sa.Column('error', sa.VARCHAR(), nullable=True),
        sa.ForeignKeyConstraint(['registration_id'], ['workflow_registrations.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_workflow_executions_id', 'workflow_executions', ['id'], unique=False
    )
    op.create_index(
        'ix_workflow_executions_registration_id',
        'workflow_executions',
        ['registration_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_workflow_executions_registration_id', table_name='workflow_executions')
    op.drop_index('ix_workflow_executions_id', table_name='workflow_executions')
    op.drop_table('workflow_executions')
    op.drop_index('ix_workflow_registrations_workflow_id', table_name='workflow_registrations')
    op.drop_index('ix_workflow_registrations_id', table_name='workflow_registrations')
    op.drop_table('workflow_registrations')
