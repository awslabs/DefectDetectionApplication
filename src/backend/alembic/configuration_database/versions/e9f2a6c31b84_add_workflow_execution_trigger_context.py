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

"""add workflow execution trigger context column

Additive-only migration for the Trigger Activation Runtime feature
(design D6): adds the nullable ``trigger_context_json`` Text column to
the existing ``workflow_executions`` table so triggered runs persist the
Trigger_Context that activated them (Requirement 6.8). The column is
nullable with no default — existing rows and manual-trigger runs stay
NULL — so existing devices upgrade in place without a data migration and
no existing column is modified.

Revision ID: e9f2a6c31b84
Revises: d3a7b1e94f26
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9f2a6c31b84'
down_revision: Union[str, None] = 'd3a7b1e94f26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The single column this migration adds: the JSON-serialized
#: Trigger_Context of the trigger firing that activated the run; NULL for
#: manual and pre-existing runs.
COLUMN_NAME = 'trigger_context_json'


def _existing_columns() -> set:
    """The column names currently on ``workflow_executions`` (empty when
    the table is somehow absent)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {col["name"] for col in inspector.get_columns("workflow_executions")}
    except Exception:  # noqa: BLE001 - table missing => nothing to guard against
        return set()


def upgrade() -> None:
    # Idempotent, mirroring the observability-columns migration
    # (d3a7b1e94f26): only add the column when it isn't already present, so
    # a device that already carries it (soft-deploy / state drift) survives
    # the deploy's startup ``alembic upgrade head`` instead of failing with
    # "duplicate column name".
    if COLUMN_NAME in _existing_columns():
        return
    with op.batch_alter_table('workflow_executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN_NAME, sa.TEXT(), nullable=True))


def downgrade() -> None:
    # Symmetric guard: only drop the column when it is actually present.
    if COLUMN_NAME not in _existing_columns():
        return
    with op.batch_alter_table('workflow_executions', schema=None) as batch_op:
        batch_op.drop_column(COLUMN_NAME)
