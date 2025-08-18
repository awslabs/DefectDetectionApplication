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

from marshmallow import ValidationError
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR
import os
import time
import json

from .image_source_configuration_accessor import ImageSourceConfigurationAccessor
from dao.sqlite_db import image_source_dao
from dao.sqlite_db import image_source_configuration_dao
from model.image_source import ImageSourceSchema, ImageSourceType
from utils import utils, constants, dda_user_management_utils
from edge_ml1_p_camera_management import aravis_functions

# TODO: Uncomment for scaling to api and preview. These are the only two places we create and delete camera source.
from utils.camera_manager import (
    connect_camera,
    disconnect_camera,
    get_camera_status,
    CameraStatusEnum
)

import logging
logger = logging.getLogger(__name__)

class ImageSourceAccessor:
    def __init__(self):
        self.schema = ImageSourceSchema()
        with open(constants.DEFAULT_CAMERA_CONFIG_FILE_PATH, "r") as jsonFile:
            self.default_camera_config = json.load(jsonFile)
        self.image_source_config_accessor = ImageSourceConfigurationAccessor()

    def create_image_source(self, data, db: Session):
        try:
            # TODO: make image source name unique
            image_source_id = utils.gen_uuid()
            data["imageSourceId"] = image_source_id
            current_ts = int(time.time() * 1000)
            data["creationTime"] = current_ts
            data["lastUpdateTime"] = current_ts
            data["imageCapturePath"] = ""

            # Create image src config and output path for Camera type
            # Create directory for Folder type
            if data.get("type") == ImageSourceType.CAMERA.value:
                img_src_cfg_id = self.__create_image_source_configuration(
                    data.get("imageSourceConfiguration"),
                    data.get("cameraId"),
                    db
                )
                # Store image source configuration id only
                data["imageSourceConfigId"] = img_src_cfg_id
                imageCapturePath = constants.IMAGE_CAPTURE_DIR + "/" + image_source_id
                data["imageCapturePath"] = imageCapturePath
                self.__create_folder(imageCapturePath)
            elif data.get("type") == ImageSourceType.FOLDER.value:
                self.__create_folder(data.get("location"))
            ## DD-18130: Add support for smart cameras
            elif data.get("type") == ImageSourceType.ICAM.value:
                imageCapturePath = constants.IMAGE_CAPTURE_DIR + "/" + image_source_id
                data["imageCapturePath"] = imageCapturePath
                self.__create_folder(imageCapturePath)
            result = self.schema.load(data)
            image_source_dao.create_image_source(db, self.schema.dump(result))
            logger.info("Stored image source with id:" + str(image_source_id))

            return {"imageSourceId": getattr(result, "imageSourceId")}
        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="The server can't create the image source. Error: '{}'. Check the error message and try again.".format(
                    err.messages
                ),
            )

    def list_image_sources(self, type, db: Session):
        logger.info("Inside Image Sources")
        return image_source_dao.list_image_sources(db, type)
    
    def list_image_source_ids_by_camera(self, camera_id, db: Session):
        return image_source_dao.list_image_source_ids_by_camera(db, camera_id)

    def get_image_source(self, id, db: Session):
        image_source = image_source_dao.get_image_source(db, id)
        if not image_source:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"The server can't find the image source. Error: 'The image source {id} doesn't exist'. Check the image source ID and try again.",
            )
        else:
            return image_source

    def update_image_source(self, id, data, db: Session):
        try:
            original_image_source = image_source_dao.get_image_source(db, id)
            if not original_image_source:
                raise HTTPException(
                    status_code=HTTP_404_NOT_FOUND,
                    detail=f"The server can't find the image source. Error: 'The image source {id} doesn't exist'. Check the image source ID and try again.",
                )
            
            # Remove the original camera object, this will disconnect the camera and remove the object
            original_image_source_dict = utils.convert_sqlalchemy_object_to_dict(original_image_source)
            if original_image_source_dict.get("type") == ImageSourceType.CAMERA:
                original_camera_id = original_image_source_dict.get('cameraId')
                # DD-18239: Check if camera is used by other image sources. If not, then disconnect camera
                connected_image_sources = self.list_image_source_ids_by_camera(original_camera_id, db)
                if len(connected_image_sources) == 1 \
                    and connected_image_sources[0] == original_image_source_dict["imageSourceId"]:
                    disconnect_camera(original_camera_id)

            current_ts = int(time.time() * 1000)
            data["imageSourceId"] = id
            data["lastUpdateTime"] = current_ts
            if data.get("imageSourceConfiguration"):
                img_src_cfg_id = self.__create_image_source_configuration(
                    data.get("imageSourceConfiguration"),
                    data.get("cameraId"),
                    db
                )
                # Store image source configuration id only
                data["imageSourceConfigId"] = img_src_cfg_id
                del data["imageSourceConfiguration"]
            errors = self.schema.validate(data, partial=True)

            # TODO: is checking this and raising here makes sense? Refactor this
            if errors:
                logger.error(errors)
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=f"The server can't update the image source. Error:  'Failed to validate image source configuration. {errors}'. Check image source configuration provided and try again",
                )

            # Create the new folder or just passthrough if its pass in as the same for some reason.
            if data.get("location"):
                self.__create_folder(data.get("location"))

            image_source_dao.update_image_source(db, data, id)
            logger.info("Updated image source with id:" + str(id))

            # Connect to the camera after update
            updated_image_source = image_source_dao.get_image_source(db, id)
            camera_id = updated_image_source.cameraId
            if updated_image_source.type == ImageSourceType.CAMERA \
                and get_camera_status(camera_id).status == CameraStatusEnum.DISCONNECTED:
                connect_camera(camera_id)
            return {"imageSourceId": id}

        except ValidationError as err:
            logger.error(err.messages)
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="The server can't update the image source. Error: 'Failed to validate image source configuration: {}'. Check image source configuration provided and try again".format(
                    err.messages
                ),
            )
        except ValueError as err:
            logger.error(err)
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"The server can't update the image source. Error: 'The image source {id} doesn't exist'. Check the image source ID and try again.",
            )

    def delete_image_source(self, id, db: Session):
        try:
            image_source = image_source_dao.get_image_source(db, id)
            image_source_dao.delete_image_source(db, id)

            # If we have a camera source we first clear it from db so incase of an error a restart won't recreate the 
            # camera object again in case of an error. 
            image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
            if image_source_dict and image_source_dict.get("type") == ImageSourceType.CAMERA:
                original_camera_id = image_source_dict.get('cameraId')
                # DD-18239: Check if camera is used by other image sources. If not, then disconnect camera
                connected_image_sources = self.list_image_source_ids_by_camera(original_camera_id, db)
                if not connected_image_sources:
                    disconnect_camera(original_camera_id)
            return {"imageSourceId": id}

        except ValueError as err:
            logger.error(err)
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=f"The server can't delete the image source. Error: 'The image source {id} doesn't exist'. Check the image source ID and try again.",
            )
        except Exception as err:
            logger.error(err)
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"The server can't delete the image source. Error: 'The image source {id} cannot be deleted. Restart the application to cleanup resources"
            )

    def __create_folder(self, folder_path):
        # Require folder path to be absolute path
        if folder_path and not os.path.isabs(folder_path):
            raise ValidationError(
                "Folder path is required and should be absolute path: {}".format(folder_path)
            )
        return dda_user_management_utils.create_dda_user_directory(folder_path)

    def __create_image_source_configuration(self, image_src_config, cameraId, db: Session):
        logger.info("Creating image source configuration: {}".format(image_src_config))
        if not image_src_config:
            image_src_config = self.__get_default_image_source_configuration(cameraId)
        config_id = self.image_source_config_accessor.create_image_source_configuration(
            db, image_src_config
        )
        return config_id

    def __get_default_image_source_configuration(self, cameraId):
        if cameraId is None:
            raise ValidationError("CameraId is required")

        # Fetch make and model of camera by CameraID
        camera = aravis_functions.getCamera(cameraId)
        cameraVendor = camera.get_vendor_name()
        cameraModel = camera.get_model_name()

        # Auto-select known camera config based on brand/model
        cameraVendor = cameraVendor if cameraVendor in self.default_camera_config else "default"
        cameraModel = cameraModel if cameraModel in self.default_camera_config[cameraVendor] else "default"
        return {
            "gain": 1,
            "exposure": 500,
            "processingPipeline": self.default_camera_config.get(cameraVendor).get(cameraModel).get("processingPipeline")
        }
    
    def update_all_image_sources_with_camera_status(self, image_sources):
        new_image_sources = []
        for image_source in image_sources: 
            image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
            new_image_sources.append(self.update_image_source_with_camera_status(image_source_dict))
        return new_image_sources

    def update_image_source_with_camera_status(self, image_source_dict):
        if image_source_dict.get('type') == ImageSourceType.FOLDER:
            image_source_dict["cameraStatus"] = None
        else:
            camera_id = image_source_dict.get('cameraId', None)
            image_source_dict["cameraStatus"] = get_camera_status(camera_id)
        return image_source_dict
    
    def list_cameras_used_by_image_sources(self, db: Session):
        # List all cameras currently being used
        # This function returns a list of unique camera names added as image sources to the station
        saved_cameras = set()
        for image_source in self.list_image_sources(ImageSourceType.CAMERA, db):
            image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
            saved_cameras.add(image_source_dict.get('cameraId'))
        return list(saved_cameras)
