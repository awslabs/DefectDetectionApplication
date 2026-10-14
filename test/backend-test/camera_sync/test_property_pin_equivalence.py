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
"""Property test for cloud/device pin equivalence.

# Feature: cloud-static-camera-provisioning, Property 10: Cloud/device pin
# equivalence

*For any* valid image file, applying it through the StaticImagePinWorker
leaves the device in a state indistinguishable from a direct
Device_Pin_API pin of the same file: identical on-disk store state,
identical pin-status metadata (width, height, format, file name),
identical camera-enumeration identity fields for ``static-image-camera``,
byte-identical ``get_frame`` output (data, width, height, pixel format),
and identical observable results for any subsequent Device_Pin_API replace
or remove operation.

**Validates: Requirements 3.1, 3.2, 3.3, 3.7, 3.8, 7.1**

Model-based: the worker application (over a temp-dir store with fake S3 /
shadow / clock collaborators) is compared against a direct
``StaticImageStore.pin_bytes`` of the same file on a second temp-dir
store. The wall clock the store stamps into the metadata sidecar is frozen
(module-name rebinding, never the global ``time`` module) so byte-exact
disk comparison is meaningful. Enumeration goes through the WIRED
``aravis_functions.getCameras()`` with an empty physical bus double (the
``mock_gi`` conftest stub makes the module importable).

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile
import types
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

import edge_ml1_p_camera_management.aravis_functions as aravis_functions
import utils.static_image_camera as static_image_camera
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
    StaticImageStore,
)

from pin_worker_support import (
    FakeClock,
    FakeS3Client,
    FakeShadowAccessor,
    FakeSleep,
    TEST_BUCKET,
    disk_state,
    fresh_store_dirs,
    image_specs,
    make_worker,
    observable_state,
    pin_desired,
    render_image_bytes,
)

#: Frozen wall clock for the store's ``pinnedAtEpochMs`` metadata stamp so
#: the two pins produce byte-identical metadata sidecars.
_FROZEN_EPOCH_SECONDS = 1_730_000_000.0
_frozen_time = types.SimpleNamespace(time=lambda: _FROZEN_EPOCH_SECONDS)

_file_names = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=32,
)

# A subsequent Device_Pin_API operation performed identically on both
# stores after the initial pin (Requirement 3.8): replace or remove.
_followups = st.one_of(
    st.none(),
    st.tuples(st.just("replace"), image_specs),
    st.tuples(st.just("remove"), st.none()),
)


class _EmptyAravisBus:
    """Physical-bus double with zero devices: the enumeration result is
    exactly the static camera's contribution."""

    @staticmethod
    def enable_interface(name):
        pass

    @staticmethod
    def update_device_list():
        pass

    @staticmethod
    def get_n_devices():
        return 0


def _identity_fields(camera):
    """The seven identity fields of one enumeration entry (Req 3.2)."""
    return (
        camera.id,
        camera.model,
        camera.address,
        camera.physical_id,
        camera.protocol,
        camera.serial,
        camera.vendor,
    )


def _enumerate(store):
    """The wired camera enumeration with ``store`` installed."""
    with patch.object(aravis_functions, "Aravis", _EmptyAravisBus), patch.object(
        aravis_functions, "get_store", lambda: store
    ):
        return [_identity_fields(camera) for camera in aravis_functions.getCameras()]


def _apply_followup(store, op, payload, file_name):
    """One Device_Pin_API operation; returns ``(result, error message)``."""
    try:
        if op == "replace":
            return store.pin_bytes(payload, file_name), None
        store.unpin()
        return None, None
    except Exception as exc:  # noqa: BLE001 - the error is the observable
        return None, "{}: {}".format(type(exc).__name__, exc)


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    spec=image_specs,
    file_name=_file_names,
    followup=_followups,
)
def test_cloud_device_pin_equivalence(prior_spec, spec, file_name, followup):
    """# Feature: cloud-static-camera-provisioning, Property 10:
    Cloud/device pin equivalence

    **Validates: Requirements 3.1, 3.2, 3.3, 3.7, 3.8, 7.1**
    """
    cloud_dir = tempfile.mkdtemp(prefix="pin-equiv-cloud-")
    direct_dir = tempfile.mkdtemp(prefix="pin-equiv-direct-")
    try:
        cloud_store_dir, marker_path = fresh_store_dirs(cloud_dir)
        direct_store_dir = direct_dir + "/store"
        cloud_store = StaticImageStore(base_dir=cloud_store_dir)
        direct_store = StaticImageStore(base_dir=direct_store_dir)

        payload = render_image_bytes(*spec)
        desired = pin_desired("req-equiv-1", payload, file_name=file_name)

        with patch.object(static_image_camera, "time", _frozen_time):
            # Same starting state on both sides — a pre-existing
            # Pinned_Image exercises the atomic-replacement path (3.1, 7.1).
            if prior_spec is not None:
                prior = render_image_bytes(*prior_spec)
                cloud_store.pin_bytes(prior, "prior.img")
                direct_store.pin_bytes(prior, "prior.img")

            # Cloud-initiated: the worker cycle over the fake transport.
            clock = FakeClock()
            worker = make_worker(
                cloud_store,
                FakeShadowAccessor(),
                FakeS3Client({(TEST_BUCKET, desired["key"]): payload}),
                clock,
                FakeSleep(clock),
                marker_path,
            )
            report = worker.process_one(desired)

            # Device-initiated: the Device_Pin_API's own primitive.
            direct_metadata = direct_store.pin_bytes(payload, file_name)

            # The confirmation carries the same metadata a direct pin
            # reports (3.3), and the application succeeded.
            assert report["status"] == "applied"
            assert report["metadata"] == direct_metadata

            # Identical on-disk store state, pin-status output, and
            # byte-identical frames (3.1, 3.3, 3.7).
            assert disk_state(cloud_store_dir) == disk_state(direct_store_dir)
            assert cloud_store.status() == direct_store.status()
            assert observable_state(cloud_store) == observable_state(direct_store)

            # Identical enumeration identity fields under the fixed id
            # (3.2): both sides enumerate exactly one static entry whose
            # seven fields equal the fixed identity.
            cloud_cameras = _enumerate(cloud_store)
            direct_cameras = _enumerate(direct_store)
            assert cloud_cameras == direct_cameras
            assert cloud_cameras == [
                (
                    STATIC_IMAGE_CAMERA_ID,
                    STATIC_IMAGE_CAMERA_IDENTITY["model"],
                    STATIC_IMAGE_CAMERA_IDENTITY["address"],
                    STATIC_IMAGE_CAMERA_IDENTITY["physical_id"],
                    STATIC_IMAGE_CAMERA_IDENTITY["protocol"],
                    STATIC_IMAGE_CAMERA_IDENTITY["serial"],
                    STATIC_IMAGE_CAMERA_IDENTITY["vendor"],
                )
            ]

            # A subsequent Device_Pin_API replace/remove behaves
            # identically on a cloud-initiated Pinned_Image (3.8).
            if followup is not None:
                op, followup_spec = followup
                followup_payload = (
                    render_image_bytes(*followup_spec)
                    if followup_spec is not None
                    else None
                )
                cloud_result = _apply_followup(
                    cloud_store, op, followup_payload, "followup.img"
                )
                direct_result = _apply_followup(
                    direct_store, op, followup_payload, "followup.img"
                )
                assert cloud_result == direct_result
                assert disk_state(cloud_store_dir) == disk_state(direct_store_dir)
                assert observable_state(cloud_store) == observable_state(
                    direct_store
                )
                assert _enumerate(cloud_store) == _enumerate(direct_store)
    finally:
        shutil.rmtree(cloud_dir, ignore_errors=True)
        shutil.rmtree(direct_dir, ignore_errors=True)
