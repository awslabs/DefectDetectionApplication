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
from utils import constants, utils
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
)

import json
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


def migration_static_camera_pipeline_db(session):
    """
    This method will do the backfill for:
        * converging the conversion chain stored for Static_Image_Camera Image_Sources

    The classic Image_Source path resolves its GStreamer conversion chain by camera
    vendor at CREATION time and PERSISTS it, so an Image_Source created before the
    static camera got its own `AWS-DDA` entry in default_camera_configurations.json
    still stores the `default`/`default` BGGR Bayer chain and keeps feeding
    bayer2rgb already-packed RGB frames.

    Scoped tightly on purpose: only a `Camera` Image_Source whose cameraId is the
    static camera AND whose stored processingPipeline is EXACTLY the known-wrong
    `default`/`default` string is rewritten. Both strings are read from
    default_camera_configurations.json rather than hardcoded, so the two halves of
    the fix cannot drift. Any other stored value — including a deliberate user
    customization — and every physical camera's configuration are left alone, and a
    second run rewrites nothing.
    """
    try:
        with open(constants.DEFAULT_CAMERA_CONFIG_FILE_PATH, "r") as json_file:
            default_camera_config = json.load(json_file)

        # The known-wrong value: the shipped default/default fall-through.
        known_wrong_pipeline = (
            default_camera_config.get("default", {})
                                 .get("default", {})
                                 .get("processingPipeline")
        )
        # The replacement: the static camera's own entry, keyed by the shipped
        # enumeration identity so a future identity change cannot silently
        # resurrect the Bayer default here.
        static_camera_entry = default_camera_config.get(
            STATIC_IMAGE_CAMERA_IDENTITY.get("vendor"), {})
        replacement_pipeline = (
            static_camera_entry.get(STATIC_IMAGE_CAMERA_IDENTITY.get("model"))
            or static_camera_entry.get("default")
            or {}
        ).get("processingPipeline")

        if not known_wrong_pipeline or not replacement_pipeline:
            logger.warning(
                "Skipping static camera pipeline backfill: could not resolve both the "
                "known-wrong ({}) and the replacement ({}) conversion chain from {}".format(
                    known_wrong_pipeline, replacement_pipeline,
                    constants.DEFAULT_CAMERA_CONFIG_FILE_PATH))
            return
        if known_wrong_pipeline == replacement_pipeline:
            return

        image_sources = image_source_dao.list_image_sources(session, ImageSourceType.CAMERA)
        rewritten_image_source_ids = []
        for img_src in image_sources:
            if img_src.cameraId != STATIC_IMAGE_CAMERA_ID:
                continue
            image_source_config = img_src.imageSourceConfiguration
            if image_source_config is None:
                continue
            if image_source_config.processingPipeline != known_wrong_pipeline:
                continue
            image_source_config.processingPipeline = replacement_pipeline
            rewritten_image_source_ids.append(img_src.imageSourceId)

        if rewritten_image_source_ids:
            session.flush()
            logger.info(
                "Static camera pipeline backfill rewrote the stored processingPipeline to "
                "{} for Image_Source(s): {}".format(
                    replacement_pipeline, ", ".join(rewritten_image_source_ids)))
    except Exception as e:
        logger.warning(e)
        pass


def backfill():
    with SessionLocal() as session:
        with session.begin():
            migration_cleanup_imgsrc_db(session)
            migration_cleanup_workflow_db(session) 
            migration_static_camera_pipeline_db(session)
        # inner context calls session.commit(), if there were no exceptions
    # outer context calls session.close()
