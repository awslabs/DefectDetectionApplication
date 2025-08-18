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
import gi
gi.require_version('Gst', '1.0')
gi.require_version('Aravis', '0.8')
from gi.repository import Aravis

from metrics.collector import Timer
import logging
import logging
logger = logging.getLogger(__name__)
import multiprocessing
from multiprocessing.managers import BaseManager
import pickle
from utils.namespace_lock import NamespaceLock
import queue
import concurrent.futures
from utils.common import CameraStatusEnum
from data_models.common import CameraStatusModel
import time
from exceptions.api.aravis_camera_exception import AravisCameraException

from threading import Lock

get_frame_lock = Lock()

class Camera():
    def __init__(self,camera_id):
        self.status = None
        Aravis.enable_interface("Fake")
        self.camera_id = camera_id
        logger.info(f"Camera ID {self.camera_id} : init")
        self._lock = multiprocessing.Lock()
        self._lock.acquire()
        self.camera = self.connect_camera()
        if self.camera and self.set_camera():
            self.payload = self.camera.get_payload()
            self.stream = self.camera.create_stream(None, None)
            self.set_buffer()
            self.update_camera_status(CameraStatusEnum.CONNECTED)
        self._lock.release()

    def get_status(self):
        return self.status
    
    def update_camera_status(self, status, error = None):
        self.status = CameraStatusModel(status=status, lastUpdatedTime=time.time(), error=str(error))

    def disconnect(self):
        # Cleanup and release resources here
        logger.info(f"Camera ID {self.camera_id} : Disconnecting")
        self._lock.acquire()
        if self.camera:
            with Timer(metric_name="CameraDisconnectTime"):
                try:
                    self.camera.stop_acquisition()
                except Exception as e:
                    logger.error(f"Error while disconnecting camera {self.camera_id}: {e}")
                finally:
                    self.unset_camera()
                    if hasattr(self, "stream"):
                        del self.stream
                    if hasattr(self, "camera"):
                        del self.camera
                    Aravis.shutdown()
        self._lock.release()

    def get_camera_id(self):
        return self.camera_id
        
    def connect_camera(self):
        logger.info(f"Camera ID {self.camera_id} : Connecting")
        try:
            with Timer(metric_name="CameraConnectTime"):
                return Aravis.Camera.new(self.camera_id)
        except TypeError as e:
            logger.info(f"No camera found {self.camera_id}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)
        except Exception as e:
            logger.error(f"Unable to connect camera {self.camera_id}. {e}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)
    def set_camera(self):
        try: 
            logger.info(f"Camera ID {self.camera_id} : Setup camera")
            device = self.camera.get_device()

            # TODO: This is ideal settings but didnt work with zebra cameras.
            # device.set_string_feature_value("TriggerMode", "On")
            # device.set_string_feature_value("TriggerSelector", "FrameStart")
            # device.set_string_feature_value("AcquisitionMode", "SingleFrame")
            # device.set_string_feature_value("TriggerSource", "Software")

            # Worked with zebra, basler and omrom
            device.set_string_feature_value("TriggerMode", "On")
            device.set_string_feature_value("TriggerSource", "Software")
            device.set_string_feature_value("AcquisitionMode", "Continuous")
            logger.info("camera setup done")
            return True
        except Exception as e:
            logger.error(f"Unable to set camera {self.camera_id}: {e}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)


    ## Use this function to unset any camera configuration changes that may impact 
    ## other 3rd party tools from accessing the camera
    def unset_camera(self):
        logger.info(f"Camera ID {self.camera_id} : Reverting camera settings")
        try:
            device = self.camera.get_device()
            # Disable trigger mode
            device.set_string_feature_value("TriggerMode", "Off")
            logger.info("Reverting camera settings done")
        except Exception as e:
            logger.error(f"Unable to unset camera trigger mode settings. {e}")
            self.update_camera_status(CameraStatusEnum.DISCONNECTED, e)

    def set_buffer(self):
        self.stream.push_buffer(Aravis.Buffer.new_allocate(self.payload))

    def start_acquisition(self, image_source_config):
        # Made image_src_cfg optional for camera status check
        self._lock.acquire()
        if image_source_config:
            # TODO: For fake camera set pixel type. for real cameras its expected to setup externally. This need to be tested.
            self.gain = image_source_config.get("gain")
            self.exposure = image_source_config.get("exposure")
            logger.info(f"setting gain {self.gain}")
            self.camera.set_gain(self.gain)
            logger.info(f"setting exposure {self.exposure}")
            self.camera.set_exposure_time(self.exposure)
            logger.info(f"Camera ID {self.camera_id} : camera setup done, start acquisition")
        with Timer(metric_name="CameraStartAcquisitionTime"):
            self.camera.start_acquisition()
        self._lock.release()

    def stop_acquisition(self):
        self._lock.acquire()
        logger.info(f"Camera ID {self.camera_id} : stop acquisition")
        with Timer(metric_name="CameraStopAcquisitionTime"):
            self.camera.stop_acquisition()
        self._lock.release()
        logger.info(f"Camera ID {self.camera_id} : stopped acquisition")

    def get_frame(self):
        logger.info(f"Camera ID {self.camera_id} : get frame acquisition")
        self._lock.acquire()
        with Timer(metric_name="CameraGetFrameTime"):
            self.camera.software_trigger()
            arv_buffer = self.stream.pop_buffer()
            self.set_buffer()
            if arv_buffer.get_status() != Aravis.BufferStatus.SUCCESS:
                logger.error(f"Camera ID {self.camera_id} : Failed to get frame")
                self.update_camera_status(CameraStatusEnum.DISCONNECTED, "Failed to get frame")
                self._lock.release()
                return pickle.dumps(None)
            else:
                data = arv_buffer.get_data()
                wd = arv_buffer.get_image_width()
                ht = arv_buffer.get_image_height()
                #update camera connection status 
                self.update_camera_status(CameraStatusEnum.CONNECTED)
                data_dict = {'data': data, 'height': ht, 'width': wd}
                pickled_data = pickle.dumps(data_dict)
                self._lock.release()
                return pickled_data

################
# Camera Manager

manager_base = BaseManager()
manager_base.register('Camera', Camera)  
manager_base.start()

# Create a dictionary to store the Camera objects by camera_id
manager = multiprocessing.Manager()
camera_objects = manager.dict()


def get_all_camera_statuses():
    status_objs = {}
    for camera_id in camera_objects:
        status = get_camera_status(camera_id)
        status_objs[camera_id] = status
    return status_objs

def get_camera_status(camera_id):
    camera = camera_objects.get(camera_id) 
    if camera:
        return camera.get_status()
    return CameraStatusModel(status=CameraStatusEnum.DISCONNECTED, lastUpdatedTime=time.time())

def connect_camera(camera_id):
    if not camera_id:
        raise AravisCameraException("Camera ID is required")

    if camera_id in camera_objects:
        disconnect_camera(camera_id)

    camera = manager_base.Camera(camera_id)
    camera_objects[camera_id] = camera
    camera_status = get_camera_status(camera_id)

    if camera_status.status == CameraStatusEnum.CONNECTED:
        return True
    else: # Connection Failed
        disconnect_camera(camera_id)
        raise AravisCameraException(camera_status.error)

def _disconnect_camera(camera_id):
    camera = camera_objects.get(camera_id)
    if camera:
        camera.disconnect()

def disconnect_camera(camera_id):
    logger.info(f'Deleting camera: {camera_id}')
    if camera_id in camera_objects:
        _disconnect_camera(camera_id)
        del camera_objects[camera_id]
        logger.info(f"Deleted camera {camera_id}")
    return True

def disconnect_all_cameras():
    if camera_objects:
        logger.info('Disconnecting all cameras')
        for camera_id in camera_objects:
            _disconnect_camera(camera_id)
        del camera_objects
        logger.info('Deleted all cameras')
    else:
        logger.info("No cameras found during disconnect process")

def _get_camera_frame(camera_id, camera, camera_config):
    try:
        camera.start_acquisition(camera_config)
        camera_frame = pickle.loads(camera.get_frame())
        camera.stop_acquisition()
        return camera_frame
    except Exception as err:
        disconnect_camera(camera_id)
        logger.error(f"Unable to grab frames. {err}")
        return None

def get_camera_frame(camera_id, camera_config=None):
    get_frame_lock.acquire()
    if camera_id not in camera_objects:
        logger.error("Attempting to create camera object")
        connect_camera(camera_id)

    camera = camera_objects.get(camera_id)
    if camera is None:
        logger.error(f"Camera not found for ID {camera_id}")
        raise Exception(f"Camera not able to connect for ID {camera_id}")
    try:
        frame = _get_camera_frame(camera_id, camera, camera_config)
        if frame is not None:
            return frame
        else:
            raise Exception(f"Unable to get camera frame for camera id: {camera_id}")
    except Exception:
        raise Exception(f"Unable to get camera frame for camera id: {camera_id}")
    finally:
        get_frame_lock.release()
