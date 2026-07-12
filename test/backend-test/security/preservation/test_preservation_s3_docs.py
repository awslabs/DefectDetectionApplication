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
"""B4 / B5 index.rst / s3.rst prose + structure preservation baseline
(Req 3.4, 3.5).

Spec: security-s3-bucket-squatting-fixes — Property 2: Preservation.

The B4 fix (task 3.1) adds an ownership ``.. note::`` and a documented
``head-bucket`` preflight to ``index.rst``; the B5 fix (task 3.2) replaces the
message-broker config sample ``"bucket"`` value with ``<your-bucket-name>`` and
adds a prerequisite ``.. note::`` in ``s3.rst``. Everything else — the four
``dpkg -i`` steps, the ``pip install`` step, the ``:caption:`` directives and
the toctree (index.rst); the ``region`` / ``key`` / ``overwrite`` config keys
and their descriptions (s3.rst) — MUST stay byte-for-byte identical.

Methodology (observation-first): the exact bytes of both files are recorded as
``s3_baseline_index_rst.txt`` / ``s3_baseline_s3_rst.txt``. This test asserts
the structural prose elements are present on the UNFIXED tree AND that the files
match their recorded goldens byte-for-byte. Task 8 re-runs the structural
assertions against the fixed tree (the full-file equality assertions are relaxed
there to hold out only the added note / placeholder lines).

**Validates: Requirements 3.4, 3.5**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_s3_docs.py \
        -p no:cacheprovider --noconftest -v
"""
import os

from _s3_preservation_support import (
    baseline_path,
    read_repo_file,
    INDEX_RST_REL,
    S3_RST_REL,
)

INDEX_GOLDEN = baseline_path("s3_baseline_index_rst.txt")
S3_GOLDEN = baseline_path("s3_baseline_s3_rst.txt")


def _read_golden(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# B4 — index.rst structure
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.4 — the four dpkg -i dependency steps are present.
def test_index_rst_four_dpkg_steps_present():
    src = read_repo_file(INDEX_RST_REL)
    for pkg in ("aws-c-iot.deb", "aws-crt-cpp.deb",
                "aws-iot-device-sdk-cpp-v2.deb", "aws-sdk-cpp.deb"):
        assert f"dpkg -i {pkg}" in src, f"missing dpkg -i step for {pkg}"


# Validates: Requirements 3.4 — the pip install wheel step and captions/toctree.
def test_index_rst_pip_install_captions_and_toctree_present():
    src = read_repo_file(INDEX_RST_REL)
    assert "python3 -m pip install panorama-1.0-py3-none-any.whl" in src
    assert "dpkg -i PanoramaSDK.deb" in src
    assert ":caption: Debian Package" in src
    assert ":caption: Python Wheel" in src
    assert ".. toctree::" in src
    assert ":maxdepth: 1" in src
    # The toctree component entries.
    for entry in ("components/gst_application", "components/properties",
                  "components/message_broker/message_broker"):
        assert entry in src, f"missing toctree entry {entry}"


# Validates: Requirements 3.4 — full-file byte-for-byte capture on unfixed tree.
def test_index_rst_matches_golden():
    assert read_repo_file(INDEX_RST_REL) == _read_golden(INDEX_GOLDEN), (
        "index.rst drifted from the recorded baseline text"
    )


# --------------------------------------------------------------------------- #
# B5 — s3.rst structure
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.5 — the region/key/overwrite config keys + descriptions.
def test_s3_rst_config_keys_and_descriptions_present():
    src = read_repo_file(S3_RST_REL)
    # The three documented config-key sections.
    assert "-   **bucket**" in src
    assert "-   **key**" in src
    assert "-   **overwrite**" in src
    # Their descriptions.
    assert "The bucket where the artifact will be uploaded" in src
    assert 'Whether or not to overwrite the bucket/key combination' in src
    assert '"region": "us-west-2"' in src
    # After the B5 fix the config sample bucket is the obvious placeholder
    # (F' — the predictable literal is gone); the surrounding structure is
    # otherwise byte-for-byte identical (asserted by the golden test below).
    assert '"bucket": "<your-bucket-name>"' in src
    assert '"bucket": "panorama-sdk-v2-artifacts"' not in src


# Validates: Requirements 3.5 — full-file byte-for-byte capture on unfixed tree.
def test_s3_rst_matches_golden():
    assert read_repo_file(S3_RST_REL) == _read_golden(S3_GOLDEN), (
        "s3.rst drifted from the recorded baseline text"
    )
