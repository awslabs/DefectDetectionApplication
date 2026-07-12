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
"""B2 / B3 publish.sh targets preservation baseline (Req 3.2, 3.3).

Spec: security-s3-bucket-squatting-fixes — Property 2: Preservation.

``publish.sh`` uploads the freshly built ``.deb`` / ``.whl`` packages (versioned
+ ``latest``) to ``panorama-sdk-v2-artifacts`` and syncs the Sphinx docs to
``edgeml-sdk-docs`` under ``edgeml-sdk/v1/$major_minor/``, guarded by
``if [ -d "./sphinx" ]``. On the UNFIXED tree these buckets are hardcoded
literals. The B2/B3 fix (task 4) parameterizes them via ``ARTIFACT_BUCKET`` /
``DOCS_BUCKET`` env vars DEFAULTING to the current literals and adds a
``head-bucket`` preflight — so with the env vars UNSET the resolved targets, the
versioned + ``latest`` key layout, the docs path, and the guard MUST stay
byte-for-byte identical.

Methodology (observation-first): capture ``F(X)`` — the resolved bucket literals
and the exact upload / sync / guard lines — on the UNFIXED tree. Task 8 re-runs
this and asserts that with env vars unset the resolution is unchanged.

**Validates: Requirements 3.2, 3.3**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_s3_publish.py \
        -p no:cacheprovider --noconftest -v
"""
import json

from _s3_preservation_support import (
    baseline_path,
    resolve_publish_targets,
    ARTIFACT_BUCKET_LITERAL,
    DOCS_BUCKET_LITERAL,
)

BASELINE = baseline_path("s3_baseline_publish_targets.json")


def _load_baseline():
    with open(BASELINE, encoding="utf-8") as fh:
        return json.load(fh)


# Validates: Requirements 3.2 — the artifact bucket resolves to the current
# literal on the unfixed tree (env var unset).
def test_artifact_bucket_resolves_to_current_literal():
    b = _load_baseline()
    assert b["artifact_bucket"] == ARTIFACT_BUCKET_LITERAL
    live = resolve_publish_targets()
    assert live["artifact_bucket"] == ARTIFACT_BUCKET_LITERAL


# Validates: Requirements 3.3 — the docs bucket resolves to the current literal.
def test_docs_bucket_resolves_to_current_literal():
    b = _load_baseline()
    assert b["docs_bucket"] == DOCS_BUCKET_LITERAL
    live = resolve_publish_targets()
    assert live["docs_bucket"] == DOCS_BUCKET_LITERAL


# Validates: Requirements 3.2 — the four .deb/.whl uploads (versioned + latest).
def test_versioned_and_latest_deb_whl_upload_layout():
    b = _load_baseline()
    cp_lines = b["cp_lines"]
    # Exactly four cp uploads: .deb versioned, .deb latest, .whl versioned, .whl latest.
    assert len(cp_lines) == 4, f"expected 4 cp uploads, got {cp_lines}"

    deb_versioned = [c for c in cp_lines if "*.deb" in c and "/release/$version/" in c]
    deb_latest = [c for c in cp_lines if "*.deb" in c and "/release/latest/" in c]
    whl_versioned = [c for c in cp_lines if ".whl" in c and "/release/$version/" in c]
    whl_latest = [c for c in cp_lines if ".whl" in c and "/release/latest/" in c]

    assert deb_versioned and deb_latest, "missing versioned/latest .deb uploads"
    assert whl_versioned and whl_latest, "missing versioned/latest .whl uploads"
    # The `latest` .deb upload is renamed to PanoramaSDK.deb.
    assert any("PanoramaSDK.deb" in c for c in deb_latest)
    # All uploads target the artifact bucket.
    for c in cp_lines:
        assert f"s3://{ARTIFACT_BUCKET_LITERAL}/release/" in c

    # Live tree matches the recorded lines exactly.
    live = resolve_publish_targets()
    assert live["cp_lines"] == cp_lines


# Validates: Requirements 3.3 — the docs-sync path and the ./sphinx guard.
def test_docs_sync_path_and_sphinx_guard():
    b = _load_baseline()
    assert b["docs_path"] == "edgeml-sdk/v1/$major_minor/"
    assert b["docs_sync_line"] == (
        "aws s3 sync ./sphinx s3://edgeml-sdk-docs/edgeml-sdk/v1/$major_minor/"
    )
    assert any('if [ -d "./sphinx" ]' in ln for ln in b["guard_lines"])

    live = resolve_publish_targets()
    assert live["docs_path"] == b["docs_path"]
    assert live["docs_sync_line"] == b["docs_sync_line"]
    assert live["guard_lines"] == b["guard_lines"]


# Validates: Requirements 3.2, 3.3 — full-file byte-for-byte capture.
def test_full_publish_sh_text_matches_golden():
    b = _load_baseline()
    live = resolve_publish_targets()
    assert live["full_text"] == b["full_text"], (
        "publish.sh drifted from the recorded baseline text"
    )
