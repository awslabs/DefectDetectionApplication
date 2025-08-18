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

"""Initial migration for database_configuration

Revision ID: 25e57085e48f
Revises: 
Create Date: 2024-05-01 19:51:49.884276

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine.reflection import Inspector

from utils.constants import GPIO_FALLING, GPIO_RISING

# revision identifiers, used by Alembic.
revision: str = '25e57085e48f'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def table_exists(engine, table_name):
    # Migrate from SQLAlchemy to Alembic, handle existing tables
    inspector = Inspector.from_engine(engine)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not table_exists(bind, "workflow"):
        op.create_table('image_source_configuration',
        sa.Column('imageSourceConfigId', sa.VARCHAR(), nullable=False),
        sa.Column('gain', sa.INTEGER(), nullable=True),
        sa.Column('exposure', sa.INTEGER(), nullable=True),
        sa.Column('processingPipeline', sa.VARCHAR(), nullable=True),
        sa.Column('creationTime', sa.INTEGER(), nullable=True),
        sa.Column('imageCrop', sqlite.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('imageSourceConfigId')
        )
        op.create_index('ix_image_source_configuration_imageSourceConfigId', 'image_source_configuration', ['imageSourceConfigId'], unique=False)
        op.create_table('input_configuration',
        sa.Column('inputConfigurationId', sa.VARCHAR(), nullable=False),
        sa.Column('creationTime', sa.INTEGER(), nullable=True),
        sa.Column('pin', sa.VARCHAR(), nullable=False),
        sa.Column('triggerState', sa.Enum(GPIO_RISING, GPIO_FALLING, name="enum_digital_to_signal_type"), nullable=False),
        sa.Column('debounceTime', sa.INTEGER(), nullable=False),
        sa.PrimaryKeyConstraint('inputConfigurationId')
        )
        op.create_index('ix_input_configuration_inputConfigurationId', 'input_configuration', ['inputConfigurationId'], unique=False)
        op.create_table('image_source',
        sa.Column('imageSourceId', sa.VARCHAR(), nullable=False),
        sa.Column('name', sa.VARCHAR(), nullable=True),
        sa.Column('type', sa.VARCHAR(length=6), nullable=True),
        sa.Column('location', sa.VARCHAR(), nullable=True),
        sa.Column('cameraId', sa.VARCHAR(), nullable=True),
        sa.Column('description', sa.VARCHAR(), nullable=True),
        sa.Column('creationTime', sa.INTEGER(), nullable=True),
        sa.Column('lastUpdateTime', sa.INTEGER(), nullable=True),
        sa.Column('imageCapturePath', sa.VARCHAR(), nullable=True),
        sa.Column('imageSourceConfigId', sa.VARCHAR(), nullable=True),
        sa.ForeignKeyConstraint(['imageSourceConfigId'], ['image_source_configuration.imageSourceConfigId'], ),
        sa.PrimaryKeyConstraint('imageSourceId')
        )
        op.create_index('ix_image_source_imageSourceId', 'image_source', ['imageSourceId'], unique=False)
        op.create_table('output_configuration',
        sa.Column('outputConfigurationId', sa.VARCHAR(), nullable=False),
        sa.Column('pin', sa.VARCHAR(), nullable=False),
        sa.Column('signalType', sa.Enum(GPIO_RISING, GPIO_FALLING, name="enum_digital_to_signal_type"), nullable=False),
        sa.Column('pulseWidth', sa.INTEGER(), nullable=False),
        sa.Column('creationTime', sa.INTEGER(), nullable=True),
        sa.Column('rule', sa.VARCHAR(length=7), nullable=False),
        sa.PrimaryKeyConstraint('outputConfigurationId')
        )
        op.create_index('ix_output_configuration_outputConfigurationId', 'output_configuration', ['outputConfigurationId'], unique=False)
        op.create_table('workflow',
        sa.Column('workflowId', sa.VARCHAR(), nullable=False),
        sa.Column('name', sa.VARCHAR(), nullable=True),
        sa.Column('description', sa.VARCHAR(), nullable=True),
        sa.Column('creationTime', sa.INTEGER(), nullable=True),
        sa.Column('lastUpdatedTime', sa.INTEGER(), nullable=True),
        sa.Column('workflowOutputPath', sa.VARCHAR(), nullable=True),
        sa.Column('featureConfigurations', sqlite.JSON(), nullable=True),
        sa.Column('inputConfigurations', sqlite.JSON(), nullable=True),
        sa.Column('outputConfigurations', sqlite.JSON(), nullable=True),
        sa.Column('imageSourceId', sa.VARCHAR(), nullable=True),
        sa.ForeignKeyConstraint(['imageSourceId'], ['image_source.imageSourceId'], ),
        sa.PrimaryKeyConstraint('workflowId')
        )
        op.create_index('ix_workflow_workflowId', 'workflow', ['workflowId'], unique=False)
    else:
        pass


def downgrade() -> None:
    bind = op.get_bind()
    if table_exists(bind, "workflow"):
        op.drop_index('ix_workflow_workflowId', table_name='workflow')
        op.drop_table('workflow')
    if table_exists(bind, "output_configuration"):
        op.drop_index('ix_output_configuration_outputConfigurationId', table_name='output_configuration')
        op.drop_table('output_configuration')
    if table_exists(bind, "image_source"):
        op.drop_index('ix_image_source_imageSourceId', table_name='image_source')
        op.drop_table('image_source')
    if table_exists(bind, "input_configuration"):
        op.drop_index('ix_input_configuration_inputConfigurationId', table_name='input_configuration')
        op.drop_table('input_configuration')
    if table_exists(bind, "image_source_configuration"):
        op.drop_index('ix_image_source_configuration_imageSourceConfigId', table_name='image_source_configuration')
        op.drop_table('image_source_configuration')