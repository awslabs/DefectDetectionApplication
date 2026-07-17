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
"""Property test for the Aravis stable Camera_Source id derivation.

**Feature: aravis-camera-input, Property 3: Aravis stable id determinism**

*For any* Aravis camera identity (vendor, model, serial), the derived
stable id SHALL be a pure function of those fields — invariant under bus
enumeration order, runtime id changes, and address changes — and distinct
identities within an enumeration SHALL derive distinct stable ids.

Exercised both directly on the pure :func:`camera_discovery.aravis
.aravis_stable_id` derivation and end-to-end through
:func:`camera_discovery.aravis.enumerate_aravis` over fake buses whose
runtime attributes (Aravis runtime id, address, protocol) and enumeration
order are re-randomized between passes.

**Validates: Requirements 2.2**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
from types import SimpleNamespace

from hypothesis import given
from hypothesis import strategies as st

from camera_discovery.aravis import (
    STABLE_ID_PREFIX,
    aravis_stable_id,
    enumerate_aravis,
)

# --- generators --------------------------------------------------------------

# Printable ASCII excluding "|", matching real GenICam vendor/model/serial
# strings (the derivation joins its identity fields with "|", and no bus
# reports a pipe inside an identity field).
_TEXT_ALPHABET = st.characters(
    min_codepoint=32, max_codepoint=126, exclude_characters="|"
)

_VENDORS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)
_MODELS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)
_SERIALS = st.text(alphabet=_TEXT_ALPHABET, max_size=24)  # may be empty (2.2 fallback)
_PHYSICAL_IDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=24)

# Bus-stable identity: (vendor, model, serial, physical_id).
_IDENTITIES = st.tuples(_VENDORS, _MODELS, _SERIALS, _PHYSICAL_IDS)

# Runtime attributes the Aravis bus does NOT keep stable across reconnects.
_RUNTIME_IDS = st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=32)
_ADDRESSES = st.text(alphabet=_TEXT_ALPHABET, max_size=32)
_PROTOCOLS = st.sampled_from(["GigEVision", "USB3Vision", "Fake"])


def _identity_key(identity):
    """The bus-stable identity the derivation keys on.

    ``physical_id`` participates only through the empty-serial fallback,
    so two identities differing only in ``physical_id`` while carrying the
    same non-empty serial are the SAME identity to the derivation.
    """
    vendor, model, serial, physical_id = identity
    if serial:
        return (vendor, model, serial)
    return (vendor, model, serial, physical_id)


# Distinct identities as an enumeration would report them.
_ENUMERATED_IDENTITIES = st.lists(
    _IDENTITIES, min_size=1, max_size=6, unique_by=_identity_key
)


@st.composite
def _fake_bus_passes(draw):
    """Two enumeration passes over the same identities where everything
    non-identity changes between passes: enumeration order, Aravis runtime
    id, address, and protocol."""
    identities = draw(_ENUMERATED_IDENTITIES)

    def fake_pass(ordered):
        return [
            SimpleNamespace(
                id=draw(_RUNTIME_IDS),
                model=model,
                address=draw(_ADDRESSES),
                physical_id=physical_id,
                protocol=draw(_PROTOCOLS),
                serial=serial,
                vendor=vendor,
            )
            for vendor, model, serial, physical_id in ordered
        ]

    first = fake_pass(identities)
    second = fake_pass(draw(st.permutations(identities)))
    return identities, first, second


# --- properties ---------------------------------------------------------------


@given(identity=_IDENTITIES, other_physical_id=_PHYSICAL_IDS)
def test_stable_id_is_a_pure_function_of_the_identity_fields(
    identity, other_physical_id
):
    """**Feature: aravis-camera-input, Property 3: Aravis stable id
    determinism**

    **Validates: Requirements 2.2**
    """
    vendor, model, serial, physical_id = identity

    stable_id = aravis_stable_id(vendor, model, serial, physical_id)

    # Deterministic: re-deriving the same identity yields the same id.
    assert aravis_stable_id(vendor, model, serial, physical_id) == stable_id

    # Shape: arv- prefix followed by a 12-hex-digit digest.
    assert stable_id.startswith(STABLE_ID_PREFIX)
    digest = stable_id[len(STABLE_ID_PREFIX):]
    assert len(digest) == 12
    assert all(char in "0123456789abcdef" for char in digest)

    if serial:
        # With a serial present the id is a function of (vendor, model,
        # serial) only — physical_id never participates.
        assert aravis_stable_id(vendor, model, serial, other_physical_id) == stable_id
    elif other_physical_id != physical_id:
        # Empty-serial fallback: physical_id keeps two serial-less
        # cameras of the same vendor/model distinct.
        assert aravis_stable_id(vendor, model, serial, other_physical_id) != stable_id


@given(bus=_fake_bus_passes())
def test_stable_id_invariant_under_bus_order_runtime_id_and_address_changes(bus):
    """**Feature: aravis-camera-input, Property 3: Aravis stable id
    determinism**

    **Validates: Requirements 2.2**
    """
    identities, first_pass, second_pass = bus

    first = enumerate_aravis(lambda: first_pass)
    second = enumerate_aravis(lambda: second_pass)
    assert first.failures == []
    assert second.failures == []
    assert len(first.cameras) == len(second.cameras) == len(identities)

    def ids_by_identity(result):
        return {
            _identity_key(
                (camera.vendor, camera.model, camera.serial, camera.physical_id)
            ): camera.stable_id
            for camera in result.cameras
        }

    first_ids = ids_by_identity(first)
    second_ids = ids_by_identity(second)

    # Invariance: the identity -> stable id mapping survives enumeration
    # reordering and runtime id / address / protocol churn (2.2).
    assert first_ids == second_ids

    # The enumerated id equals the pure derivation of the identity fields
    # alone — runtime attributes cannot have participated.
    for camera in first.cameras:
        assert camera.stable_id == aravis_stable_id(
            camera.vendor, camera.model, camera.serial, camera.physical_id
        )


@given(bus=_fake_bus_passes())
def test_distinct_identities_within_an_enumeration_derive_distinct_ids(bus):
    """**Feature: aravis-camera-input, Property 3: Aravis stable id
    determinism**

    **Validates: Requirements 2.2**
    """
    identities, first_pass, _ = bus

    result = enumerate_aravis(lambda: first_pass)
    assert result.failures == []

    stable_ids = [camera.stable_id for camera in result.cameras]
    assert len(set(stable_ids)) == len(identities)
