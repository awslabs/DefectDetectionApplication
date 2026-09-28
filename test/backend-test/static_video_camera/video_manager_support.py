# Copyright 2025 Amazon Web Services, Inc.
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
"""Import helpers and doubles for the static-video-camera integration tests.

``import_camera_manager`` mirrors the static-image-camera suite's helper:
``utils.camera_manager`` starts a ``BaseManager`` server at import time,
which works on the flask-app images (``fork``) and is stubbed out on hosts
where it cannot start. ``FakeAravisBus`` stands in for ``aravis_functions``'
module-level ``Aravis`` binding with a generated physical camera list.
"""
import sys
from unittest.mock import patch

from hypothesis import strategies as st


def import_camera_manager():
    """Return the ``utils.camera_manager`` module, importable anywhere."""
    if "utils.camera_manager" in sys.modules:
        return sys.modules["utils.camera_manager"]
    try:
        import utils.camera_manager as camera_manager
        return camera_manager
    except Exception:
        sys.modules.pop("utils.camera_manager", None)
        with patch("multiprocessing.managers.BaseManager.start"), \
                patch("multiprocessing.Manager") as manager_factory:
            manager_factory.return_value.dict.return_value = {}
            import utils.camera_manager as camera_manager
        return camera_manager


class FakeAravisBus:
    """The enumeration entry points ``getCameras()``/``rescan_cameras()``
    touch, backed by a list of physical camera field dicts."""

    def __init__(self, physical):
        self.physical = list(physical)

    def enable_interface(self, name):
        pass

    def update_device_list(self):
        pass

    def shutdown(self):
        pass

    def get_n_devices(self):
        return len(self.physical)

    def get_device_id(self, i):
        return self.physical[i]["id"]

    def get_device_model(self, i):
        return self.physical[i]["model"]

    def get_device_address(self, i):
        return self.physical[i]["address"]

    def get_device_physical_id(self, i):
        return self.physical[i]["physical_id"]

    def get_device_protocol(self, i):
        return self.physical[i]["protocol"]

    def get_device_serial_nbr(self, i):
        return self.physical[i]["serial"]

    def get_device_vendor(self, i):
        return self.physical[i]["vendor"]


CAMERA_FIELDS = ("id", "model", "address", "physical_id", "protocol", "serial", "vendor")


def camera_fields(camera):
    return tuple(getattr(camera, field) for field in CAMERA_FIELDS)


def physical_fields(entry):
    return tuple(entry[field] for field in CAMERA_FIELDS)


def _physical_camera(index):
    return {
        "id": "Vendor{}-SN{:04d}".format(index % 3, index),
        "model": "Model-{}".format(index),
        "address": "10.0.0.{}".format(index + 10),
        "physical_id": "phys-{}".format(index),
        "protocol": "GigEVision",
        "serial": "SN{:04d}".format(index),
        "vendor": "Vendor{}".format(index % 3),
    }


physical_camera_lists = st.lists(
    st.integers(min_value=0, max_value=50), max_size=4, unique=True
).map(lambda indices: [_physical_camera(i) for i in indices])
