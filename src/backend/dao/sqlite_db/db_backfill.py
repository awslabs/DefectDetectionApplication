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

from .sqlite_db_operations import SessionLocal
from .models import Workflow, ImageSource
from sqlalchemy import select, update
from dao.sqlite_db import workflow_dao, image_source_dao
from model.image_source import ImageSourceType
from utils import utils

import logging

logger = logging.getLogger(__name__)


def migration_cleanup_imgsrc_db(session):
    """
    This method will do the backfill for:
        * cleaning up the image capture path of Folder ImgSrc in the image_source db
    """
    image_sources = image_source_dao.list_image_sources(session, ImageSourceType.FOLDER)
    update_list = []
    for img_src in image_sources:
        img_src_id = img_src.imageSourceId
        update_list.append({"imageSourceId": img_src_id, "imageCapturePath": ""})
    session.execute(update(ImageSource), update_list)


def migration_cleanup_workflow_db(session):
    """
    This method will do the backfill for:
        * cleaning up the default configuration in the workflow db
        * update the model name with model friendly name
    """
    # for db before the backfill, this method will list all workflow info including the default configs
    workflows = workflow_dao.list_workflows(session)
    update_list = []
    for workflow in workflows:
        if workflow.featureConfigurations:
            feature_configs = workflow.featureConfigurations[0]
            feature_configs_without_default_config = {
                'modelName': feature_configs.get("modelName"),
                'type': feature_configs.get("type")
            }
            update_list.append({"workflowId": workflow.workflowId, "featureConfigurations": [feature_configs_without_default_config]})
    try:
        session.execute(update(Workflow), update_list)
    except Exception as e:
        logger.warning(e)
        pass


def backfill():
    with SessionLocal() as session:
        with session.begin():
            migration_cleanup_imgsrc_db(session)
            migration_cleanup_workflow_db(session) 
        # inner context calls session.commit(), if there were no exceptions
    # outer context calls session.close()
