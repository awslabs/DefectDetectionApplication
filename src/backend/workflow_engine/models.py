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

"""SQLAlchemy models for the workflow engine (additive only).

New tables live in the configuration database (control-plane data,
same database as the existing ``workflow`` table) and are created by the
alembic migration ``a4f8c2d91e57_create_workflow_engine_tables`` in
``alembic/configuration_database/versions``. No existing table is
modified (Requirement 13.5).
"""

from sqlalchemy import Column, ForeignKey, Integer, String

from dao.sqlite_db.sqlite_db_operations import Base


class WorkflowRegistration(Base):
    """A workflow discovered from a deployed Workflow_Component.

    One row per (workflow_id, version, arch) artifact set found under
    /aws_dda/workflows/. ``status`` is 'registered' for runnable
    workflows and 'invalid' for malformed/incompatible artifacts
    (registered but never runnable, Requirement 9.1).
    """

    __tablename__ = "workflow_registrations"

    id = Column(String, primary_key=True, index=True)
    workflow_id = Column(String, nullable=False, index=True)
    version = Column(String, nullable=False)
    arch = Column(String, nullable=False)
    artifact_path = Column(String, nullable=False)
    status = Column(String, nullable=False)
    registered_at = Column(Integer, nullable=False)


class WorkflowExecution(Base):
    """A single run of a registered workflow.

    ``failing_node_id`` and ``error`` are populated when a run fails,
    mapping the failing GStreamer element back to its workflow node
    (Requirement 9.7).
    """

    __tablename__ = "workflow_executions"

    id = Column(String, primary_key=True, index=True)
    registration_id = Column(
        String, ForeignKey("workflow_registrations.id"), nullable=False, index=True
    )
    started_at = Column(Integer)
    finished_at = Column(Integer)
    status = Column(String, nullable=False)
    failing_node_id = Column(String)
    error = Column(String)
