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

from sqlalchemy.orm import Session
from sqlalchemy import select

from .models import Workflow
from data_models.common import WorkflowModel


def get_workflow(db: Session, workflow_id: str):
    workflow = db.get(Workflow, workflow_id)
    # workflow.imageSources is needed to flush imageSources back
    # TODO: further investigation and remove below
    if workflow:
        image_source = workflow.imageSources
    return workflow


def list_workflows(db: Session):
    return db.scalars(select(Workflow)).all()

def list_workflows_ids_by_image_sources(image_souce_ids, db:Session):
    return db.scalars(select(Workflow.workflowId).filter(Workflow.imageSourceId.in_(image_souce_ids))).all()

def create_workflow(db: Session, workflow: WorkflowModel):
    db_workflow = Workflow(**workflow)
    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


def delete_workflow(db: Session, workflow_id: str):
    workflow = db.get(Workflow, workflow_id)
    if not workflow:
        raise ValueError(f"Workflow with id {workflow_id} does not exist.")
    db.delete(workflow)
    db.commit()


def update_workflow(db: Session, workflow: WorkflowModel, workflow_id: str):
    db_workflow = db.query(Workflow).filter(Workflow.workflowId == workflow_id)
    if not db_workflow:
        raise ValueError(f"Workflow with id {workflow_id} does not exist.")
    db_workflow.update(workflow)
    db.commit()