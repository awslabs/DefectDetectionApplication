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

# System Modules
from fastapi import Depends, Query
from sqlalchemy.orm import Session
from dao.sqlite_db.sqlite_db_operations import SessionLocal, engine
from dao.sqlite_db import image_source_configuration_dao
import data_models.common as schemas
import dao.sqlite_db.models as models
import os
from utils import utils

# Fast api
from fastapi import HTTPException, APIRouter, Depends
from pydantic import conint, BaseModel, RootModel, validator
from typing import List, Optional, Literal
from typing_extensions import Annotated
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR

# Custom Modules
from exceptions.api.unexpected_type_exception import UnexpectedTypeException
from utils.server_setup import gst_pipeline_executor, image_source_accessor, image_src_cfg_accessor
from model.image_source_configuration import ImageSourceConfigurationSchema
from model.image_source import ImageSource, ImageSourceType
from utils import captured_images_utils, utils
from utils.constants import CAPTURED_IMAGE_FOLDER_PATTHERN, CAPTURED_IMAGE_FILE_PATH_PATTHERN
import logging
logger = logging.getLogger(__name__)
from data_models.common import (
    ImageSourceModel,
    ImageSourceIdModel,
    ImageSourceConfigurationsInputModel,
    ImageSourceConfigurationsOutputModel,
    CapturedImageModel,
)
from utils.camera_manager import get_camera_frame
from endpoints.route.access_log_router import get_api_router

router = get_api_router()


# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ListImageSourceConfigsResponse(RootModel):
    root: List[ImageSourceConfigurationsOutputModel]


@router.get("/image-source-configurations")
def read_image_source_configurations(db: Session = Depends(get_db)) -> ListImageSourceConfigsResponse:
    users = image_src_cfg_accessor.list_image_source_configurations(db)
    return users


class GetPreviewImageRequest(BaseModel):
    imageSourceConfiguration: dict = {}


class GetPreviewImageResponse(BaseModel):
    image: Optional[str] = None
    imageFileName: Optional[str] = None

    @validator('imageFileName', always=True)
    def check_image_or_image_file_name(cls, imageFileName, values):
        if not values.get('image') and not imageFileName:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="The server cannot preview image from camera or find image file in folder. Check the error message and try again.",
            )
        return imageFileName

def get_frame(image_source_dict, image_source_config_override=None):
    # Uses AravisSDK to fetch the frame from the camera
    # Only works with GenICam cameras
    if image_source_dict.get('type') in [ImageSourceType.ICAM, ImageSourceType.NVIDIA_CSI, ImageSourceType.FOLDER]:
        raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"The server cannot get frame for {image_source_dict.get('type')} using AravisSDK method.",
            )

    camera_config = {}
    if image_source_config_override:
        camera_config = image_source_config_override
    else:
        camera_config = utils.convert_sqlalchemy_object_to_dict(image_source_dict.get("imageSourceConfiguration"))
    camera_id = image_source_dict.get('cameraId')
    return get_camera_frame(camera_id, camera_config)

@router.post("/image-sources/{imageSourceId}/preview")
def preview_image(imageSourceId, request: GetPreviewImageRequest = GetPreviewImageRequest(), db: Session = Depends(get_db)) -> GetPreviewImageResponse:
    try:
        image_source = image_source_accessor.get_image_source(imageSourceId, db)
        image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
        if image_source_dict.get('type') == ImageSourceType.FOLDER:
            image_path = captured_images_utils.get_oldest_image_file_path(image_source.location)
            folder_path, file_name = utils.split_file_name_and_path(image_path)
            return GetPreviewImageResponse(imageFileName=file_name)

        elif image_source_dict.get('type') == ImageSourceType.CAMERA or image_source_dict.get('type') == ImageSourceType.ICAM or image_source_dict.get('type') == ImageSourceType.NVIDIA_CSI:
            image_source_config_override = {}
            image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
            if request and request.imageSourceConfiguration:
                img_src_cfg = request.imageSourceConfiguration
                schema = ImageSourceConfigurationSchema()
                errors = schema.validate(img_src_cfg, partial=True)
                if errors:
                    logger.error(errors)
                    raise HTTPException(
                        HTTP_400_BAD_REQUEST,
                        f"The server can't get a preview image from the image source: {imageSourceId}. Error: '{errors}'. Check the error message and try again.",
                    )
                image_source_config_override = img_src_cfg
            ## DD-18130: Add support for smart cameras
            if image_source_dict.get('type') == ImageSourceType.ICAM or image_source_dict.get('type') == ImageSourceType.NVIDIA_CSI:
                return gst_pipeline_executor.execute_image_source_pipeline(
                    ImageSource(**image_source_dict), 
                    image_source_config_override=image_source_config_override, 
                    is_preview=True
                )
            return gst_pipeline_executor.execute_image_source_pipeline(
                ImageSource(**image_source_dict),
                image_source_config_override=image_source_config_override,
                is_preview=True,
                frame_data=get_frame(image_source_dict, image_source_config_override)
            )
    except Exception as err:
        raise HTTPException(
                    HTTP_500_INTERNAL_SERVER_ERROR,
                    f"The server can't get a preview image from the image source: {imageSourceId}. Error: '{err}'. Check the error message and try again.",
                )

class CaptureImageRequest(BaseModel):
    filePrefix: str = None


class CaptureImageResponse(BaseModel):
    image: str

    
@router.post("/image-sources/{imageSourceId}/capture")
def capture(
    imageSourceId, capture: CaptureImageRequest = CaptureImageRequest(), db: Session = Depends(get_db)
) -> CaptureImageResponse:
    image_source = image_source_accessor.get_image_source(imageSourceId, db)
    image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)

    if image_source_dict.get("type") == ImageSourceType.FOLDER:
        raise HTTPException(
                HTTP_400_BAD_REQUEST,
                f"Image capture is not available because the image source is set to retrieve images from a folder",
            )
    elif image_source_dict.get("type") == ImageSourceType.CAMERA:
        r = gst_pipeline_executor.execute_image_source_pipeline(
            ImageSource(**image_source_dict), is_preview=False, file_prefix=capture.filePrefix,
            frame_data=get_frame(image_source_dict)
        )
        return r
    elif image_source_dict.get("type") == ImageSourceType.ICAM or image_source_dict.get("type") == ImageSourceType.NVIDIA_CSI:
        r = gst_pipeline_executor.execute_image_source_pipeline(
            ImageSource(**image_source_dict), is_preview=False, file_prefix=capture.filePrefix
        )
        return r
    else:
        raise UnexpectedTypeException(f"Unexpected image source type: {image_source_dict.get('type')}", status_code=HTTP_500_INTERNAL_SERVER_ERROR)

class ListCapturedImageResponse(RootModel):
    root: List[CapturedImageModel]


@router.get("/captured-images")
def list_captured_images(
    path: str = Query(..., pattern=CAPTURED_IMAGE_FOLDER_PATTHERN),
    maxImages: conint(ge=0, le=12) = 12
    ) -> ListCapturedImageResponse:
    if not os.path.exists(path):
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"The server can't get captured Images. Error: 'No images were found in {path}'. Check the path and try again.",
        )

    return captured_images_utils.get_images(path, maxImages)


@router.delete("/captured-images")
def delete_captured_images(filePath: str = Query(..., pattern=CAPTURED_IMAGE_FILE_PATH_PATTHERN)) -> str:
    if not os.path.isfile(filePath):
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=f"The server can't delete the captured images. Error: 'No images were found in {filePath}'. Check the path and try again.",
        )

    return captured_images_utils.delete_image(filePath)


class AddImageSourceRequest(BaseModel):
    type: Optional[Literal["Camera", "Folder", "ICam", "NvidiaCSI"]] = None
    name: Optional[str] = None
    description: Optional[str] = None
    cameraId: Optional[str] = None
    location: Optional[str] = None


class AddImageSourceResponse(RootModel):
    root: ImageSourceIdModel


@router.post("/image-sources")
def add_image_source(request: AddImageSourceRequest, db: Session = Depends(get_db)) -> AddImageSourceResponse:
    return image_source_accessor.create_image_source(request.dict(exclude_unset=True), db)


class EditImageSourceRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    imageSourceConfiguration: Optional[ImageSourceConfigurationsInputModel] = {}
    location: Optional[str] = None


class UpdateImageSourceResponse(RootModel):
    root: ImageSourceIdModel


@router.patch("/image-sources/{image_source_id}")
def update_image_source(
    image_source_id: str, request: EditImageSourceRequest, db: Session = Depends(get_db)
) -> UpdateImageSourceResponse:
    return image_source_accessor.update_image_source(
        image_source_id, request.dict(exclude_unset=True), db
    )


class ListImageSourcesResponse(RootModel):
    root: List[ImageSourceModel]


@router.get("/image-sources")
def list_image_sources(
    type: Optional[Literal['Folder', 'Camera', 'ICam']] = None, db: Session = Depends(get_db)
):
    # TODO: Add ListImageSourcesResponse to response validation
    image_sources = image_source_accessor.list_image_sources(type, db)
    return image_source_accessor.update_all_image_sources_with_camera_status(image_sources)


@router.get("/image-sources/{imageSourceId}")
def get_image_source(imageSourceId: str, db: Session = Depends(get_db)):
    image_source = image_source_accessor.get_image_source(imageSourceId, db)
    image_source_dict = utils.convert_sqlalchemy_object_to_dict(image_source)
    return image_source_accessor.update_image_source_with_camera_status(image_source_dict)


@router.delete("/image-sources/{image_source_id}")
def delete_image_source(image_source_id: str, db: Session = Depends(get_db)):
    return image_source_accessor.delete_image_source(image_source_id, db)
