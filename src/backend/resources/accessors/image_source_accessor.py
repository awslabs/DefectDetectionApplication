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
from model import stream_source
from model.image_source import ImageSourceSchema, ImageSourceType, is_stream_source_type
from stream_ingest.credentials import CredentialStoreError, get_credential_store
from stream_ingest.manager import get_stream_ingest_manager
from utils import utils, constants, dda_user_management_utils
from edge_ml1_p_camera_management import aravis_functions

# TODO: Uncomment for scaling to api and preview. These are the only two places we create and delete camera source.
from utils.camera_manager import (
    connect_camera,
    disconnect_camera,
    get_camera_status,
    get_camera_feature_bounds,
    CameraStatusEnum
)
from data_models.common import CameraStatusModel

import logging
logger = logging.getLogger(__name__)

class ImageSourceAccessor:
    def __init__(self):
        self.schema = ImageSourceSchema()
        with open(constants.DEFAULT_CAMERA_CONFIG_FILE_PATH, "r") as jsonFile:
            self.default_camera_config = json.load(jsonFile)
        self.image_source_config_accessor = ImageSourceConfigurationAccessor()

    def create_image_source(self, data, db: Session, managed_stream_settings=None):
        """Create an Image_Source.

        ``managed_stream_settings`` (stream types only, never from the API)
        carries the device-managed ``credentialRef`` / ``credentialsUpdatedAt``
        the Edge_Sync_Agent applies with a Portal change.
        """
        stream_credentials = None
        try:
            # TODO: make image source name unique
            image_source_id = utils.gen_uuid()
            data["imageSourceId"] = image_source_id
            current_ts = int(time.time() * 1000)
            data["creationTime"] = current_ts
            data["lastUpdateTime"] = current_ts
            data["imageCapturePath"] = ""
            if not is_stream_source_type(data.get("type")):
                # Only stream Image_Sources have these (rtsp-rtmp-stream-cameras).
                for key in ("streamSettings", "credentials", "clearCredentials"):
                    if data.get(key) is not None:
                        raise ValidationError({key: ["applies to RTSP and RTMP image sources only"]})
                    data.pop(key, None)

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
                img_src_cfg_id = self.__create_image_source_configuration(
                    data.get("imageSourceConfiguration"),
                    "ICAM",  # Use "ICAM" as cameraId to trigger ICAM config
                    db
                )
                data["imageSourceConfigId"] = img_src_cfg_id
                imageCapturePath = constants.IMAGE_CAPTURE_DIR + "/" + image_source_id
                data["imageCapturePath"] = imageCapturePath
                logger.warning(f"ICAM CREATE DEBUG: imageSourceId={image_source_id}, imageCapturePath={imageCapturePath}")
                self.__create_folder(imageCapturePath)
            elif data.get("type") == ImageSourceType.NVIDIA_CSI.value:
                img_src_cfg_id = self.__create_image_source_configuration(
                    data.get("imageSourceConfiguration"),
                    None,
                    db
                )
                data["imageSourceConfigId"] = img_src_cfg_id
                imageCapturePath = constants.IMAGE_CAPTURE_DIR + "/" + image_source_id
                data["imageCapturePath"] = imageCapturePath
                logger.warning(f"NVIDIA CSI CREATE DEBUG: imageSourceId={image_source_id}, imageCapturePath={imageCapturePath}")
                self.__create_folder(imageCapturePath)
            elif is_stream_source_type(data.get("type")):
                stream_credentials = self.__prepare_stream_image_source(
                    image_source_id, data, db, managed_stream_settings, current_ts)
            result = self.schema.load(data)
            image_source_dao.create_image_source(db, self.schema.dump(result))
            logger.info("Stored image source with id:" + str(image_source_id))

            if is_stream_source_type(data.get("type")):
                self.__store_new_stream_credentials(image_source_id, stream_credentials, db)
                self.__notify_stream_config_changed(image_source_id)
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

    def update_image_source(self, id, data, db: Session, managed_stream_settings=None):
        try:
            original_image_source = image_source_dao.get_image_source(db, id)
            if not original_image_source:
                raise HTTPException(
                    status_code=HTTP_404_NOT_FOUND,
                    detail=f"The server can't find the image source. Error: 'The image source {id} doesn't exist'. Check the image source ID and try again.",
                )
            if is_stream_source_type(original_image_source.type):
                # Stream cameras have their own path: ``location`` is a
                # Stream_URL, not a folder, and credentials go to the
                # Credential_Store (rtsp-rtmp-stream-cameras).
                return self.__update_stream_image_source(
                    id, original_image_source, data, db, managed_stream_settings)
            for key in ("streamSettings", "credentials", "clearCredentials"):
                if data.get(key) is not None:
                    raise HTTPException(
                        status_code=HTTP_400_BAD_REQUEST,
                        detail=f"The server can't update the image source. Error: '{key} applies to RTSP and RTMP image sources only'. Check the request and try again.",
                    )
                data.pop(key, None)
            
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
            if image_source_dict and is_stream_source_type(image_source_dict.get("type")):
                # Requirement 4.7: the session stops and the credentials go.
                get_stream_ingest_manager().notify_deleted(id)
                get_credential_store().delete(id)
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

    # -- stream Image_Sources (rtsp-rtmp-stream-cameras, design component 10) --

    @staticmethod
    def __stream_settings_with_managed(base, managed_stream_settings):
        """``base`` settings with the device-managed keys the Edge_Sync_Agent
        supplied: a key given as None is removed (a cleared reference)."""
        settings = dict(base or {})
        for key, value in (managed_stream_settings or {}).items():
            if key not in stream_source.MANAGED_SETTINGS:
                continue
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value
        return settings

    def __create_stream_configuration(self, settings, db: Session):
        """A configuration row for a stream Image_Source. The camera fields
        hold inert values, so the existing schema needs no relaxation."""
        return self.image_source_config_accessor.create_image_source_configuration(
            db, {"gain": 0, "exposure": 0, "processingPipeline": "", "streamSettings": settings})

    def __prepare_stream_image_source(self, image_source_id, data, db: Session,
                                      managed_stream_settings, current_ts):
        """Validate and stage a new stream Image_Source in ``data``; returns
        the credentials to store once the row exists. Raises
        ValidationError naming the field (Requirement 4.2)."""
        source_type = data.get("type")
        requested_settings = data.pop("streamSettings", None)
        raw_credentials = data.pop("credentials", None)
        data.pop("clearCredentials", None)
        data.pop("imageSourceConfiguration", None)
        try:
            data["location"] = stream_source.validate_stream_url(source_type, data.get("location"))
            credentials = stream_source.validate_credentials(raw_credentials)
            settings = stream_source.normalize_stream_settings(
                source_type, requested_settings,
                base=self.__stream_settings_with_managed({}, managed_stream_settings))
        except stream_source.StreamSourceError as error:
            raise ValidationError(error.as_messages())
        if credentials and managed_stream_settings is None:
            # Credentials set at the station: stamp when they changed.
            settings["credentialsUpdatedAt"] = current_ts
        data["imageSourceConfigId"] = self.__create_stream_configuration(settings, db)
        image_capture_path = constants.IMAGE_CAPTURE_DIR + "/" + image_source_id
        data["imageCapturePath"] = image_capture_path
        self.__create_folder(image_capture_path)
        return credentials

    def __store_new_stream_credentials(self, image_source_id, credentials, db: Session):
        """Write a new stream Image_Source's credentials; when that fails the
        just-created row is removed, so no camera exists half-configured."""
        if not credentials:
            return
        try:
            get_credential_store().put(image_source_id, credentials)
        except CredentialStoreError as error:
            logger.error("Removing image source %s: its credentials could not be stored: %s",
                         image_source_id, error)
            try:
                image_source_dao.delete_image_source(db, image_source_id)
            except Exception as cleanup_error:  # noqa: BLE001 - best effort
                logger.error("Could not remove image source %s after the credential "
                             "failure: %s", image_source_id, cleanup_error)
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The server can't create the image source. Error: 'The camera credentials could not be stored on the device'. Check the device storage and try again.",
            )

    def __notify_stream_config_changed(self, image_source_id):
        """Restart a running session with the new configuration (Requirement
        8.11). A failure here never fails the CRUD request."""
        try:
            get_stream_ingest_manager().notify_config_changed(image_source_id)
        except Exception as error:  # noqa: BLE001 - the change is already stored
            logger.error("Stream session of image source %s could not be restarted: %s",
                         image_source_id, error)

    def __update_stream_image_source(self, id, original_image_source, data, db: Session,
                                     managed_stream_settings=None):
        """PATCH of an RTSP/RTMP Image_Source: a new Stream_URL, settings
        merged over the stored ones, and write-only credentials."""
        source_type = original_image_source.type
        requested_settings = data.pop("streamSettings", None)
        raw_credentials = data.pop("credentials", None)
        clear_credentials = bool(data.pop("clearCredentials", False))
        data.pop("imageSourceConfiguration", None)
        stored_configuration = original_image_source.imageSourceConfiguration
        stored_settings = (getattr(stored_configuration, "streamSettings", None) or {}) \
            if stored_configuration is not None else {}
        try:
            if data.get("location") is not None:
                data["location"] = stream_source.validate_stream_url(source_type, data["location"])
            credentials = stream_source.validate_credentials(raw_credentials)
            if credentials and clear_credentials:
                raise stream_source.StreamSourceError(
                    "credentials", "cannot be set while clearCredentials is true")
            base = self.__stream_settings_with_managed(stored_settings, managed_stream_settings)
            settings = stream_source.normalize_stream_settings(source_type, requested_settings, base=base)
        except stream_source.StreamSourceError as error:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="The server can't update the image source. Error: '{}'. Check the request and try again.".format(
                    error.as_messages()),
            )
        current_ts = int(time.time() * 1000)
        if (credentials or clear_credentials) and managed_stream_settings is None:
            # A change made at the station no longer matches a Portal
            # Credential_Reference, so the reference is dropped.
            settings.pop("credentialRef", None)
            settings["credentialsUpdatedAt"] = current_ts

        store = get_credential_store()
        previous_credentials = store.get(id)
        try:
            if credentials:
                store.put(id, credentials)
            elif clear_credentials:
                store.delete(id)
        except CredentialStoreError as error:
            logger.error("Image source %s: the credentials could not be stored: %s", id, error)
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The server can't update the image source. Error: 'The camera credentials could not be stored on the device'. Check the device storage and try again.",
            )

        try:
            data["imageSourceId"] = id
            data["lastUpdateTime"] = current_ts
            data["imageSourceConfigId"] = self.__create_stream_configuration(settings, db)
            errors = self.schema.validate(data, partial=True)
            if errors:
                raise ValidationError(errors)
            image_source_dao.update_image_source(db, data, id)
        except Exception:
            # Keep the store consistent with the unchanged row.
            if credentials or clear_credentials:
                try:
                    if previous_credentials:
                        store.put(id, previous_credentials)
                    else:
                        store.delete(id)
                except CredentialStoreError as error:
                    logger.error("Image source %s: the previous credentials could not be "
                                 "restored: %s", id, error)
            raise
        logger.info("Updated stream image source with id:" + str(id))
        self.__notify_stream_config_changed(id)
        return {"imageSourceId": id}

    @staticmethod
    def stream_camera_status(health):
        """``cameraStatus`` of a stream Image_Source from its Stream_Health:
        connected while streaming, otherwise disconnected with the last
        (redacted) error."""
        health = health or {}
        streaming = health.get("state") == "streaming"
        last_error = health.get("lastError") or {}
        stamp_ms = health.get("lastFrameAtMs") or last_error.get("atMs")
        return CameraStatusModel(
            status=CameraStatusEnum.CONNECTED if streaming else CameraStatusEnum.DISCONNECTED,
            lastUpdatedTime=(stamp_ms / 1000.0) if stamp_ms else time.time(),
            error=None if streaming else (last_error.get("message") or health.get("state")),
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
        # For Nvidia CSI cameras, use default Nvidia CSI config with nvarguscamerasrc
        if cameraId is None:
            nvidia_csi_config = {
                "gain": 1,
                "exposure": 500,
                "processingPipeline": self.default_camera_config.get("Nvidia CSI").get("default").get("processingPipeline"),
                "device": self.default_camera_config.get("Nvidia CSI").get("default").get("device"),
                "deviceName": self.default_camera_config.get("Nvidia CSI").get("default").get("deviceName")
            }
            logger.warning(f"NVIDIA CSI CONFIG DEBUG: {nvidia_csi_config}")
            return nvidia_csi_config
        
        # For ICAM cameras (cameraId starts with "ICAM"), use ICAM config
        if cameraId and cameraId.startswith("ICAM"):
            icam_config = {
                "gain": 1,
                "exposure": 500,
                "processingPipeline": self.default_camera_config.get("ICAM").get("default").get("processingPipeline"),
                "device": self.default_camera_config.get("ICAM").get("default").get("device"),
                "deviceName": self.default_camera_config.get("ICAM").get("default").get("deviceName")
            }
            logger.warning(f"ICAM CONFIG DEBUG: {icam_config}")
            return icam_config

        # Fetch make and model of camera by CameraID
        camera = aravis_functions.getCamera(cameraId)
        cameraVendor = camera.get_vendor_name()
        cameraModel = camera.get_model_name()

        # Auto-select known camera config based on brand/model
        cameraVendor = cameraVendor if cameraVendor in self.default_camera_config else "default"
        cameraModel = cameraModel if cameraModel in self.default_camera_config[cameraVendor] else "default"

        # Seed gain/exposure from the camera's ACTUAL current values so the
        # edit page's initial numbers reflect the device instead of an
        # arbitrary constant (500 us was presented as the camera's value even
        # though it was never read from it). Best-effort and read-only:
        # get_camera_feature_bounds only reads from an existing connection —
        # it never opens/claims the device — and returns {} when the camera
        # is not connected, in which case the legacy defaults stand.
        gain, exposure = 1, 500
        try:
            bounds = get_camera_feature_bounds(cameraId) or {}
            device_gain = (bounds.get("gain") or {}).get("current")
            device_exposure = (bounds.get("exposure") or {}).get("current")
            if isinstance(device_gain, (int, float)):
                gain = device_gain
            if isinstance(device_exposure, (int, float)):
                exposure = device_exposure
        except Exception as e:
            logger.warning(
                f"Could not seed image source config from camera {cameraId} "
                f"current values; using defaults: {e}"
            )
        return {
            "gain": gain,
            "exposure": exposure,
            "processingPipeline": self.default_camera_config.get(cameraVendor).get(cameraModel).get("processingPipeline")
        }
    
    def update_all_image_sources_with_camera_status(self, image_sources):
        new_image_sources = []
        for image_source in image_sources: 
            image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
            new_image_sources.append(self.update_image_source_with_camera_status(image_source_dict))
        return new_image_sources

    def update_image_source_with_camera_status(self, image_source_dict):
        if is_stream_source_type(image_source_dict.get('type')):
            # rtsp-rtmp-stream-cameras: the status comes from the session's
            # Stream_Health, and the credentials are reported only as a flag.
            image_source_id = image_source_dict.get('imageSourceId')
            health = get_stream_ingest_manager().health_for_image_source(image_source_id)
            image_source_dict["cameraStatus"] = self.stream_camera_status(health)
            image_source_dict["streamHealth"] = health
            image_source_dict["credentialsConfigured"] = get_credential_store().configured(image_source_id)
        elif image_source_dict.get('type') == ImageSourceType.FOLDER or image_source_dict.get('type') == ImageSourceType.NVIDIA_CSI or image_source_dict.get('type') == ImageSourceType.ICAM:
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
