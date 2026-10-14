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
"""Property test for fixed identifier invariance (task 4.3).

**Feature: static-image-camera-source, Property 3: Fixed identifier
invariance**

*For any* sequence of pin, replace, and restart (fresh store instance
over the same directory) operations and any generated set of physical
camera identifiers, the Static_Image_Camera identifier is
character-for-character identical after every operation and never equals
any physical camera identifier.

**Validates: Requirements 2.4**

The identifier is observed through the WIRED
``aravis_functions.getCameras()`` enumeration (the surface Image_Source
records and workflow camera bindings resolve against), not by reading
the constant back: after every operation the static entry's ``id`` must
be identical to every previously observed value and distinct from all
generated physical identifiers. Restart is modeled exactly as Property 11
does — a fresh ``StaticImageStore`` over the same directory.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

import edge_ml1_p_camera_management.aravis_functions as aravis_functions
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
)

from static_image_strategies import (
    FakeAravisBus,
    image_specs,
    physical_camera_lists,
    render_image_bytes,
)

# Pin (initial or replace — a pin over an existing pin IS the replace
# operation) and restart, each carrying an image spec for the pin case.
_operations = st.lists(
    st.tuples(st.sampled_from(["pin", "restart"]), image_specs),
    min_size=1,
    max_size=5,
)


def _observed_static_id(physical):
    """The static entry's identifier as enumeration exposes it."""
    cameras = aravis_functions.getCameras()
    physical_ids = {p["id"] for p in physical}
    static_entries = [c for c in cameras if c.id not in physical_ids]
    assert len(static_entries) == 1
    return static_entries[0].id


@settings(deadline=None)
@given(
    physical=physical_camera_lists,
    initial_spec=image_specs,
    operations=_operations,
)
def test_fixed_identifier_invariance(physical, initial_spec, operations):
    base_dir = tempfile.mkdtemp(prefix="static-image-camera-test-")
    try:
        # Mutable holder so "restart" can swap in a fresh store while the
        # patched get_store keeps resolving to the current instance.
        holder = {"store": StaticImageStore(base_dir=base_dir)}
        bus = FakeAravisBus(physical)
        physical_ids = {p["id"] for p in physical}

        with patch.object(aravis_functions, "Aravis", bus), \
                patch.object(
                    aravis_functions, "get_store", lambda: holder["store"]
                ):
            # Initial pin so the static entry exists to observe.
            holder["store"].pin_bytes(
                render_image_bytes(*initial_spec), "initial.img"
            )
            baseline_id = _observed_static_id(physical)
            assert baseline_id not in physical_ids

            for op, spec in operations:
                if op == "pin":  # initial pin already done → this replaces
                    holder["store"].pin_bytes(
                        render_image_bytes(*spec), "replacement.img"
                    )
                else:  # restart: fresh store over the same directory
                    holder["store"] = StaticImageStore(base_dir=base_dir)

                observed = _observed_static_id(physical)
                # Character-for-character identical after every operation
                # and never equal to any physical identifier (Req 2.4).
                assert observed == baseline_id
                assert observed == STATIC_IMAGE_CAMERA_ID
                assert observed not in physical_ids
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)
