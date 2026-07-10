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
"""Default-registry resolution golden (D1–D5) — Docker non-ECR base image spec
(Req 3.1).

Spec: security-docker-non-ecr-base-image-fixes — Property 2: Preservation.

Records, per in-scope ``FROM`` (D1–D5), the effective default pull reference on
the UNFIXED tree — ``nvcr.io/nvidia/<image>:<tag>`` — plus the image / tag / stage
and the manifest-list digest the fix will pin. On the unfixed tree the current
reference carries NO digest. Task 8 asserts the FIXED default-resolved reference
(``BASE_REGISTRY`` unset -> ``nvcr.io``) equals
``nvcr.io/nvidia/<image>:<tag>@sha256:<digest>`` — equivalent in effect to the
current tag pull (the pinned digest IS the manifest-list digest the tag resolves
to), so the default build pulls identical bytes.

Golden: ``baselines/docker_baseline_default_refs.json`` keyed by finding id.

**Validates: Requirements 3.1**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_docker_default_refs.py \
        -p no:cacheprovider --noconftest -v
"""
from _docker_preservation_support import (
    DIGESTS,
    IN_SCOPE_SITES,
    capture_or_assert_json,
    default_pull_reference,
    parse_from,
    read_repo_file,
)


def _parse_site(rel_path, lineno):
    """Parse the ``FROM`` at ``rel_path:lineno`` (1-based)."""
    lines = read_repo_file(rel_path).splitlines()
    assert 1 <= lineno <= len(lines), f"{rel_path}: line {lineno} out of range"
    parsed = parse_from(lines[lineno - 1])
    assert parsed is not None, (
        f"{rel_path}:{lineno} is not a FROM line: {lines[lineno - 1]!r}"
    )
    return parsed


# Validates: Requirements 3.1
def test_default_registry_resolution_golden():
    """Capture / assert the per-finding default pull reference (pre-fix)."""
    refs = {}
    for site_id, (rel_path, lineno) in sorted(IN_SCOPE_SITES.items()):
        parsed = _parse_site(rel_path, lineno)
        default_ref = default_pull_reference(parsed)
        # The digest the fix will pin (equivalent-in-effect manifest-list digest).
        pinned_digest = DIGESTS[(parsed["image"], parsed["tag"])]
        refs[site_id] = {
            "file": rel_path,
            "line": lineno,
            "image": parsed["image"],
            "tag": parsed["tag"],
            "stage": parsed["stage"],
            "default_pull_reference": default_ref,
            "fixed_default_reference": f"{default_ref}@{pinned_digest}",
        }

    # Structural sanity: the pre-fix references are the literal nvcr.io refs with
    # no digest yet.
    for site_id, rec in refs.items():
        assert rec["default_pull_reference"].startswith("nvcr.io/nvidia/"), rec
        assert "@sha256:" not in rec["default_pull_reference"], (
            f"{site_id}: pre-fix reference should carry no digest yet"
        )
        assert rec["fixed_default_reference"] == (
            rec["default_pull_reference"] + "@" + DIGESTS[(rec["image"], rec["tag"])]
        )

    # Spot-check the expected image/tag per finding.
    assert refs["D1"]["default_pull_reference"] == "nvcr.io/nvidia/l4t-jetpack:r35.4.1"
    assert refs["D2"]["default_pull_reference"] == "nvcr.io/nvidia/l4t-jetpack:r35.4.1"
    assert refs["D2"]["stage"] == "builder"
    assert refs["D3"]["default_pull_reference"] == "nvcr.io/nvidia/l4t-cuda:11.4.19-runtime"
    assert refs["D3"]["stage"] == "cuda114"
    assert refs["D4"]["default_pull_reference"] == "nvcr.io/nvidia/l4t-jetpack:r36.3.0"
    assert refs["D5"]["default_pull_reference"] == "nvcr.io/nvidia/l4t-jetpack:r36.3.0"
    assert refs["D5"]["stage"] == "builder"

    capture_or_assert_json("docker_baseline_default_refs.json", refs)
