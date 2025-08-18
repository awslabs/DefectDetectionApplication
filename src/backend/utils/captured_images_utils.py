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
import os
import glob
from fastapi import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

from model.image_source import ImageSource, ImageSourceType
from exceptions.api.captured_images_exception import CapturedImageException, ImageNotFoundException
from utils.camera_manager import get_camera_frame
from utils.constants import CAPTURE
from utils import utils

import logging
logger = logging.getLogger(__name__)

def get_images(path, max_images):

    os_path = os.listdir(path)
    captured_images = [os.path.join(path, filename) for filename in os_path if os.path.isfile(path+'/'+filename)] #Filtering only the files.
    captured_images = sorted(captured_images, key=lambda x: os.path.getmtime(x), reverse=True)[:max_images]

    images = []
    for img in captured_images:
        converted_string = utils.get_image_bytes_from_file(img)
        image_obj = {
            "path": img,
            "image": converted_string
        }
        images.append(image_obj)

    return images

def delete_image(image_path):
    filename = os.path.basename(image_path)
    os.remove(image_path)
    return filename

def get_oldest_image_file_path(location : str, throw_on_corruption : bool = True) -> str:
    image_file_list = glob.glob(location + "/*")
    image_file_list.sort(key=lambda x: os.path.getmtime(x))
    nonimage_file_list = []
    jpg_filepath = ""
    found_jpg = False
    index = 0
    while not found_jpg and index < len(image_file_list):
        image = image_file_list[index]
        if image.endswith(".jpg") or image.endswith(".jpeg"):
            jpg_filepath = image
            found_jpg = True
        else:
            nonimage_file_list.append(image)
        index += 1

    if nonimage_file_list:
        logger.warning(f"non-JPEG files found: {len(nonimage_file_list)} non-JPEG files found at path {location}")

    if not jpg_filepath:
        logger.error(f"No JPG/JPEG image files found at path {location}")
        raise ImageNotFoundException(f"No JPG/JPEG image files found at path {location}")
    elif os.path.getsize(jpg_filepath) == 0 and throw_on_corruption:
        logger.error(f"Image {jpg_filepath} is corrupted")
        raise CapturedImageException(f"Image {jpg_filepath} is corrupted")
    else:
        return jpg_filepath

def convert_captured_data_to_db(captured_data, workflow_id):
    input_image_path = captured_data.get("capturedLocation")
    capture_ts = extract_capture_time_from_file_path(input_image_path)
    db_captured_data = {
        "captureId": "{}-{}".format(captured_data.get("captureTaskId"), capture_ts),
        "inputImageFilePath": input_image_path,
        "workflowId": workflow_id,
        "inferenceCreationTime": capture_ts,
        "captureType": CAPTURE,
        "downloaded": False,
        "flagForReview": False
    }
    return db_captured_data

def extract_capture_time_from_file_path(file_path):
    timestamp = file_path.split("-")[-1]
    return timestamp.split(".")[0]

def run_image_capture_pipeline(image_source, workflow_output_path, gst_pipeline_executor, file_prefix=None):
    if image_source.get("type") == ImageSourceType.FOLDER:
        raise HTTPException(
                HTTP_400_BAD_REQUEST,
                f"Image capture is not available because the image source is set to retrieve images from a folder",
            )
    elif image_source.get("type") == ImageSourceType.CAMERA:
        r = gst_pipeline_executor.execute_image_source_pipeline(
            ImageSource(**image_source), is_preview=False, file_prefix=file_prefix,
            workflow_output_path=workflow_output_path, frame_data=get_frame(image_source)
        )
        return r.get("captureLocation")
    elif image_source.get("type") == ImageSourceType.ICAM:
        r = gst_pipeline_executor.execute_image_source_pipeline(
            ImageSource(**image_source), is_preview=False, file_prefix=file_prefix,
            workflow_output_path=workflow_output_path
        )
        return r.get("captureLocation")
    
def get_frame(image_source_dict):
    camera_config = utils.convert_sqlalchemy_object_to_dict(image_source_dict.get("imageSourceConfiguration"))
    camera_id = image_source_dict.get('cameraId')
    try:
        return get_camera_frame(camera_id, camera_config)
    except Exception as err:
        raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Server cannot fetch camera frame, Error {err}. Check camera connection and try again",
            )