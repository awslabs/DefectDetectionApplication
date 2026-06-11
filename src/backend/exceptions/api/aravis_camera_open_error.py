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
from starlette import status

from exceptions.api.aravis_camera_exception import AravisCameraException


class AravisCameraOpenError(AravisCameraException):
    """
    Raised when a camera is discovered on the bus (it shows up in the device
    list) but cannot be opened / bootstrapped.

    This is distinct from AravisCameraNotFound, which means the device was not
    enumerated at all. A common cause of an open failure is a GenICam/USB3
    camera that negotiated only a USB2 link (bad or non-SuperSpeed cable, a
    USB2 port/hub, or insufficient bus power), or the camera already being held
    open by another process. Surfacing this separately lets the UI tell the
    user the camera was detected but could not be accessed, instead of the
    misleading "not found".
    """

    def __init__(self, message, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY):
        super().__init__(message, status_code)
