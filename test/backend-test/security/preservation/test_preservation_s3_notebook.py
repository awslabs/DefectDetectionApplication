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
"""B6 notebook segmentation-manifest preservation baseline (Req 3.6).

Spec: security-s3-bucket-squatting-fixes — Property 2: Preservation.

The ``DDA_SageMaker_Model_Training_and_Compilation.ipynb`` segmentation-manifest
cell hardcodes ``old_prefix = 's3://lookoutvision-us-east-1-0e205be246/getting-
started/'`` and rewrites manifest entries under that prefix via
``update_manifest_paths``. The B6 fix (task 3.3) derives ``old_prefix`` from a
single-source ``sample_data_bucket`` variable and adds a prerequisite markdown
cell, but MUST preserve the ``update_manifest_paths`` rewrite logic, the
``wget`` of the GitHub manifest, the upload/cleanup steps, the resulting
``old_prefix`` string value, and the notebook JSON validity.

Methodology (observation-first): ``json.load`` the notebook (validity), locate
the manifest cell, and record its source + the current ``old_prefix`` string as
``s3_baseline_notebook.json``. Task 8 re-runs this and asserts the rewrite logic
and the resolved ``old_prefix`` string are unchanged.

nbformat is used when available; otherwise the recorded ``nbformat`` /
``nbformat_minor`` fields are asserted directly (the runner in this environment
does not vendor nbformat).

**Validates: Requirements 3.6**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_s3_notebook.py \
        -p no:cacheprovider --noconftest -v
"""
import json

from _s3_preservation_support import (
    baseline_path,
    load_notebook,
    find_manifest_cell,
)

BASELINE = baseline_path("s3_baseline_notebook.json")

OLD_PREFIX_LITERAL = "s3://lookoutvision-us-east-1-0e205be246/getting-started/"


def _load_baseline():
    with open(BASELINE, encoding="utf-8") as fh:
        return json.load(fh)


# Validates: Requirements 3.6 — the notebook is valid JSON with the recorded
# nbformat version (parseable by nbformat / json.load).
def test_notebook_is_valid():
    b = _load_baseline()
    nb = load_notebook()  # json.load raises if invalid
    assert nb.get("nbformat") == b["nbformat"]
    assert nb.get("nbformat_minor") == b["nbformat_minor"]
    assert len(nb.get("cells", [])) == b["n_cells"]

    # Validate through nbformat too when it is installed in the runner.
    try:
        import nbformat  # noqa: WPS433
    except ImportError:
        return
    from _s3_preservation_support import REPO_ROOT, NOTEBOOK_REL
    import os
    parsed = nbformat.read(
        os.path.join(REPO_ROOT, NOTEBOOK_REL), as_version=4
    )
    nbformat.validate(parsed)


# Validates: Requirements 3.6 — the current old_prefix literal is recorded.
def test_old_prefix_literal_recorded():
    b = _load_baseline()
    assert b["old_prefix"] == OLD_PREFIX_LITERAL


# Validates: Requirements 3.6 — the manifest cell (update_manifest_paths, wget,
# upload/cleanup steps) matches the recorded golden byte-for-byte.
def test_manifest_cell_source_matches_golden():
    b = _load_baseline()
    nb = load_notebook()
    idx, src = find_manifest_cell(nb)
    assert idx == b["manifest_cell_index"]
    assert src == b["manifest_cell_source"], (
        "segmentation-manifest cell drifted from the recorded baseline"
    )


# Validates: Requirements 3.6 — the specific logic elements that must survive.
def test_manifest_cell_logic_elements_present():
    b = _load_baseline()
    src = b["manifest_cell_source"]
    # update_manifest_paths rewrite logic.
    assert "def update_manifest_paths(manifest_file, old_prefix, new_prefix):" in src
    assert "for key in ['source-ref', 'anomaly-mask-ref']:" in src
    assert "data[key].startswith(old_prefix)" in src
    assert "data[key] = data[key].replace(old_prefix, new_prefix)" in src
    # The wget of the GitHub manifest.
    assert "wget -q https://raw.githubusercontent.com/aws-samples/amazon-lookout-for-vision" in src
    assert "train_segmentation.manifest" in src
    # Upload + cleanup steps.
    assert "s3_client.upload_file(seg_manifest_path, bucket, s3_key)" in src
    assert "os.remove('train_segmentation.manifest')" in src
    # After the B6 fix the prefix is derived from a single-source
    # ``sample_data_bucket`` variable (F') rather than a bare literal; the
    # resolved old_prefix STRING value is unchanged (asserted separately) and the
    # bare-literal assignment is gone.
    assert 'sample_data_bucket = "lookoutvision-us-east-1-0e205be246"' in src
    assert "old_prefix = f's3://{sample_data_bucket}/getting-started/'" in src
    assert f"old_prefix = '{OLD_PREFIX_LITERAL}'" not in src
    assert "update_manifest_paths('train_segmentation.manifest', old_prefix, s3_uri)" in src
