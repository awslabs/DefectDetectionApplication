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
"""README example-policy prose preservation baseline (I16, I17).

Spec: security-iam-authorization-fixes — Property 2: Preservation.

The I16 / I17 fix rewrites ONLY the two ```json example-policy code fences in
``README_main.md`` (``dda-build-policy`` and ``dda-greengrass-policy``). Every
other byte of the README — the surrounding prose, headings, numbered steps, and
all other code fences — must remain byte-for-byte identical.

Baseline: ``iam_baseline_readme_prose.md`` is the current README with the two
IAM JSON fences replaced by a stable ``<<<IAM_JSON_FENCE_EXCISED>>>`` token. The
test re-derives the same excised view from the live README and compares
byte-for-byte. On the unfixed tree this is an identity check (PASS). Task 9
re-runs it against the fixed README: because the fix only changes the fenced
JSON bodies (which are excised), the excised prose view must still match.

**Validates: Requirements 3.16, 3.17**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_readme_prose.py \
        -p no:cacheprovider --noconftest -v
"""
import os

from _iam_preservation_support import (
    read_repo_file,
    readme_prose_excise_all_json_fences,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))

README_REL = "README_main.md"
# The locator is signature-agnostic: it excises EVERY ```json fence. The I16/I17
# fix rewrites the two example-policy JSON bodies (it removes the `greengrass:*`
# wildcard the old signature keyed on), so a body-substring signature no longer
# matches on the fixed tree. README_main.md contains exactly the two IAM
# example-policy ```json fences and no other ```json blocks, so excising all of
# them holds out precisely the two policy bodies and nothing else — leaving the
# surrounding prose to be compared byte-for-byte.
EXCISION_TOKEN = "<<<IAM_JSON_FENCE_EXCISED>>>"


def _baseline_prose():
    with open(os.path.join(BASELINES, "iam_baseline_readme_prose.md"),
              encoding="utf-8") as fh:
        return fh.read()


# Validates: Requirements 3.16, 3.17 — the non-fence prose is byte-for-byte identical.
def test_readme_prose_byte_for_byte_identical():
    live = read_repo_file(README_REL)
    excised = readme_prose_excise_all_json_fences(live, EXCISION_TOKEN)
    assert excised == _baseline_prose(), (
        "README_main.md prose (outside the two IAM example-policy JSON fences) "
        "drifted from the baseline"
    )


# Validates: Requirements 3.16, 3.17 — exactly the two IAM example fences are held
# out (guards against the locator accidentally matching too much / too little).
def test_exactly_two_iam_fences_excised():
    live = read_repo_file(README_REL)
    excised = readme_prose_excise_all_json_fences(live, EXCISION_TOKEN)
    assert excised.count(EXCISION_TOKEN) == 2
    # The excised prose must not leak any of the fenced policy content.
    assert "greengrass:*" not in excised
    assert '"iot:*"' not in excised


# Validates: Requirements 3.16, 3.17 — the anchoring prose the fix must preserve
# is present in the baseline (policy names + the numbered step context).
def test_readme_prose_anchors_present():
    prose = _baseline_prose()
    assert "`dda-build-policy`" in prose
    assert "`dda-greengrass-policy`" in prose
    assert "Set up IAM Permissions and Roles" in prose
    assert "Attach S3 permissions for component downloads" in prose
