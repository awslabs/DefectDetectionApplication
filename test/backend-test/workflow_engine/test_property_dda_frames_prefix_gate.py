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
"""Property test for the ``dda_frames`` allowed-URI-prefix gate.

Exec's ``HELPERS_SOURCE`` into a fresh module namespace and drives the
gate through real ``load_bytes``/``load_image`` fetches of local files
— the gate applies identically to every scheme because it sits at the
front of the shared ``_fetch_bytes`` dispatch.

- **Feature: custom-python-source, Property 10: The prefix gate permits
  exactly the declared prefixes** — Validates: Requirements 5.1, 5.2,
  5.3
"""
import os
import tempfile
import types

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.python_bridge import HELPERS_SOURCE

#: Filesystem-safe name alphabet (no separators, no null bytes).
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def load_helpers():
    module = types.ModuleType("dda_frames")
    exec(HELPERS_SOURCE, module.__dict__)
    return module


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 10: The prefix gate permits exactly
# the declared prefixes
#
# For any list of allowed URI prefixes (including the empty list) and any
# fetch source string, a fetch through the Frame_Helpers is permitted exactly
# when the list is empty or the source starts with at least one declared
# prefix; a denied fetch raises an error naming the source and stating it is
# outside the node's allowed prefixes.
#
# **Validates: Requirements 5.1, 5.2, 5.3**
# ---------------------------------------------------------------------------


@given(
    name=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    payload=st.binary(min_size=0, max_size=64),
    data=st.data(),
)
def test_property_10_prefix_gate_permits_exactly_declared_prefixes(
    name, payload, data
):
    """**Feature: custom-python-source, Property 10: The prefix gate
    permits exactly the declared prefixes**

    **Validates: Requirements 5.1, 5.2, 5.3**
    """
    helpers = load_helpers()

    with tempfile.TemporaryDirectory() as tmp_dir:
        source = os.path.join(tmp_dir, name + ".bin")
        with open(source, "wb") as f:
            f.write(payload)

        # Each declared prefix is either a true prefix of the source
        # (any cut point, including "" and the full string) or an
        # arbitrary string that is not one.
        prefix_strategies = st.one_of(
            st.integers(min_value=0, max_value=len(source)).map(
                lambda k: source[:k]
            ),
            st.text(min_size=1, max_size=30).filter(
                lambda p: not source.startswith(p)
            ),
        )
        prefixes = data.draw(
            st.lists(prefix_strategies, min_size=0, max_size=4)
        )
        helpers._set_allowed_prefixes(prefixes)

        permitted = not prefixes or any(
            source.startswith(p) for p in prefixes
        )

        if permitted:
            # Empty declaration or a matching prefix: the fetch goes
            # through and the source is recorded (Req 5.1, 5.3).
            assert helpers.load_bytes(source) == payload
            assert source in helpers._fetched_sources()
        else:
            # No declared prefix matches: ValueError naming the source
            # and stating the prefix restriction, from load_bytes and
            # load_image alike; nothing is fetched (Req 5.1, 5.2).
            with pytest.raises(ValueError) as bytes_error:
                helpers.load_bytes(source)
            message = str(bytes_error.value)
            assert source in message
            assert "outside the node's allowed" in message

            with pytest.raises(ValueError) as image_error:
                helpers.load_image(source)
            assert source in str(image_error.value)
            assert "outside the node's allowed" in str(image_error.value)

            assert helpers._fetched_sources() == []
