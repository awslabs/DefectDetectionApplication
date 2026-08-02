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

"""add workflow execution observability columns

Additive-only migration for the Deployed Workflow Run Observability
feature: adds nullable run-observability columns to the existing
``workflow_executions`` table so existing devices upgrade in place
without a data migration (Requirements 1.4, 8.3). Every new column is
nullable (``has_image_results`` also backfills to 0/False via a server
default), so existing rows remain valid and no existing column is
modified.

Revision ID: d3a7b1e94f26
Revises: a4f8c2d91e57
Create Date: 2026-06-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3a7b1e94f26'
down_revision: Union[str, None] = 'a4f8c2d91e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The observability columns this migration adds, in creation order. Each is
#: nullable; ``has_image_results`` backfills to 0/False via a server default so
#: existing rows read as False rather than NULL.
def _new_columns() -> list:
    return [
        # Per-run capture id and artifact directory used to locate the run's
        # output images/result files.
        sa.Column('capture_id', sa.VARCHAR(), nullable=True),
        sa.Column('output_dir', sa.VARCHAR(), nullable=True),
        # Whether the run routed viewable image artifacts (drives the
        # "View results" link).
        sa.Column(
            'has_image_results',
            sa.BOOLEAN(),
            nullable=True,
            server_default=sa.text('0'),
        ),
        # JSON map {nodeId: {status, detail?}} of per-node run status.
        sa.Column('node_status_json', sa.TEXT(), nullable=True),
        # Path to the per-execution run log file.
        sa.Column('log_path', sa.VARCHAR(), nullable=True),
    ]


def _existing_columns() -> set:
    """The column names currently on ``workflow_executions`` (empty when the
    table is somehow absent)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {col["name"] for col in inspector.get_columns("workflow_executions")}
    except Exception:  # noqa: BLE001 - table missing => nothing to guard against
        return set()


def upgrade() -> None:
    # Idempotent: only add the columns that aren't already present. A device
    # may already carry these columns (e.g. after a soft-deploy that ran this
    # migration, or any state drift) while alembic re-invokes ``upgrade`` — a
    # plain ``add_column`` would then fail with "duplicate column name" and
    # break the deploy's startup ``alembic upgrade head``. Guarding each add
    # makes the upgrade safe to run against any prior state (Requirement 8.3:
    # additive and non-breaking on every device).
    existing = _existing_columns()
    to_add = [column for column in _new_columns() if column.name not in existing]
    if not to_add:
        return
    with op.batch_alter_table('workflow_executions', schema=None) as batch_op:
        for column in to_add:
            batch_op.add_column(column)


def downgrade() -> None:
    # Symmetric guard: only drop the columns that are actually present.
    existing = _existing_columns()
    to_drop = [
        name
        for name in (
            'log_path',
            'node_status_json',
            'has_image_results',
            'output_dir',
            'capture_id',
        )
        if name in existing
    ]
    if not to_drop:
        return
    with op.batch_alter_table('workflow_executions', schema=None) as batch_op:
        for name in to_drop:
            batch_op.drop_column(name)
