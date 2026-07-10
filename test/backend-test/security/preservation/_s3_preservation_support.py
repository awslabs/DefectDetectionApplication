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
"""Shared helpers for the **S3 Bucket Squatting** preservation baseline tests
(Task 2 of ``security-s3-bucket-squatting-fixes``).

These tests implement **Property 2: Preservation — F(X) = F'(X) for every
legitimate (non-bug-condition) input** (``bugfix.md`` Req 3.1–3.7, ``design.md``
"Preservation Checking" / "Testing Strategy"). Methodology: observation-first —
capture ``F(X)`` baselines on the UNFIXED tree (task 2, PASS now), then re-run
the SAME files against the FIXED tree (task 8) to prove no legitimate behavior
changed and that only the enumerated preflight / env-var / placeholder additions
appear.

This module reuses the proven low-level helpers from the sibling
``_preservation_support`` module (``REPO_ROOT``, ``read_repo_file``,
``load_module_from_path``) and adds S3-spec-specific extraction helpers:

* ``load_deploy`` / ``capture_ssm_commands`` — load the REAL ``deploy.py`` with
  boto3 stubbed (mirroring the sibling ``test_preservation_deploy_ssm.py``
  ``_capture_ssm_commands``) and capture the exact ``commands`` lists handed to
  SSM, so the ``download_edgemlsdk_release_artifacts`` list is exercised, not
  transcribed.
* ``canonical_deploy_args`` — a fixed, legitimate arg tuple for the golden.
* ``resolve_publish_targets`` — parse ``publish.sh`` and return the resolved
  bucket literals / upload target lines / docs-sync path / guard on the UNFIXED
  tree (where the buckets are hardcoded, i.e. no env-var indirection yet).
* ``load_notebook`` / ``find_manifest_cell`` — ``json.load`` the notebook and
  locate the segmentation-manifest cell.

All helpers are import-light so the tests run under
``python3 -m pytest ... --noconftest`` without pulling in the backend package.
"""
import json
import os
import re
import types
from argparse import Namespace

from _preservation_support import (  # noqa: F401
    REPO_ROOT,
    load_module_from_path,
    read_repo_file,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))

# In-scope source paths (relative to REPO_ROOT) this spec owns.
DEPLOY_REL = "src/edgemlsdk/src/test/longevity/deploy.py"
PUBLISH_REL = "src/edgemlsdk/src/utilities/publish.sh"
INDEX_RST_REL = "src/edgemlsdk/src/docs/source/index.rst"
S3_RST_REL = "src/edgemlsdk/src/docs/source/components/message_broker/s3.rst"
NOTEBOOK_REL = "DDA_SageMaker_Model_Training_and_Compilation.ipynb"

# The predictable, hardcoded literals on the UNFIXED tree (the F baseline).
ARTIFACT_BUCKET_LITERAL = "panorama-sdk-v2-artifacts"
DOCS_BUCKET_LITERAL = "edgeml-sdk-docs"

# Fake AWS account returned by the stubbed ``sts get-caller-identity`` in the
# boto3 stub. The B1 fix resolves the team-owned ``edgeml-sdk-longevity-tests``
# bucket owner from the deployer's caller identity, so the captured golden /
# reference uses this deterministic value.
STUB_CALLER_ACCOUNT = "123456789012"


def baseline_path(name):
    return os.path.join(BASELINES, name)


# --------------------------------------------------------------------------- #
# B1 — deploy.py SSM list capture (boto3 stubbed; the REAL builder runs)
# --------------------------------------------------------------------------- #
def _make_boto3_stub():
    boto3 = types.ModuleType("boto3")

    class _Creds:
        access_key = "AKIAEXAMPLE1234567"
        secret_key = "wSECRETkeyEXAMPLEabcdef1234567890EXAMPLE"
        token = "FQoTOKENexample"

    def _client(service_name=None, *a, **k):
        # The B1 fix resolves the longevity bucket owner via
        # ``session.client("sts").get_caller_identity()["Account"]``, so the sts
        # client stub must expose that call returning a deterministic account.
        if service_name == "sts":
            return types.SimpleNamespace(
                get_caller_identity=lambda *aa, **kk: {"Account": STUB_CALLER_ACCOUNT}
            )
        return types.SimpleNamespace()

    class _Session:
        region_name = "us-west-2"

        def client(self, service_name=None, *a, **k):
            return _client(service_name, *a, **k)

        def get_credentials(self):
            return _Creds()

    boto3.Session = lambda *a, **k: _Session()
    boto3.client = _client

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exc.ClientError = ClientError
    botocore.exceptions = exc
    return {"boto3": boto3, "botocore": botocore, "botocore.exceptions": exc}


def load_deploy():
    """Load the REAL ``deploy.py`` as a standalone module with boto3 stubbed."""
    return load_module_from_path(
        "deploy_s3_preservation",
        DEPLOY_REL,
        injected_modules=_make_boto3_stub(),
    )


def capture_ssm_commands(args):
    """Run ``deploy.main(args)`` with AWS side effects patched out and return the
    list of ``commands`` lists passed to SSM (download list, then mqtt list if
    ``args.mqtt``)."""
    mod = load_deploy()
    captured = []
    mod.set_aws_access_keys_from_secrets_manager = lambda: None
    mod.upload_folder_to_s3 = lambda *a, **k: None
    mod.upload_file_to_s3 = lambda *a, **k: None
    mod.DeployLongevity.create_instance = lambda self, *a, **k: "i-abc"
    mod.DeployLongevity.run_commands_via_ssm_with_retry = (
        lambda self, iid, mr, commands, creds: captured.append(list(commands))
    )
    mod.DeployLongevity.close_ssm = lambda self: None
    mod.main(args)
    return captured


def canonical_deploy_args():
    """A fixed, legitimate arg tuple (shlex-safe tokens) used for the B1 golden."""
    return Namespace(
        mqtt="mqtt", platform="aarch64", ubuntu_version="22.04",
        python_version="3.11", region="us-west-2",
        mqtt_endpoint="a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com",
        release_date="20230918", longevity_hours=72, payload_size=50,
        # B1 fix adds these owner args (default None -> resolves to the
        # documented Panorama constant / sts caller identity).
        artifacts_bucket_owner=None, longevity_bucket_owner=None,
    )


def capture_download_list(args=None):
    """Capture just the ``download_edgemlsdk_release_artifacts`` list."""
    args = args or canonical_deploy_args()
    captured = capture_ssm_commands(args)
    assert captured, "expected at least the download command list"
    return captured[0]


# --------------------------------------------------------------------------- #
# B2 / B3 — publish.sh structural resolution (UNFIXED: hardcoded literals)
# --------------------------------------------------------------------------- #
def _parse_env_defaults(lines):
    """Parse ``VAR="${VAR:-default}"`` assignment lines into a ``{VAR: default}``
    map. This is how the B2/B3 fix parameterizes the buckets: with the env var
    UNSET the value resolves to the ``:-default`` literal (the pre-fix value)."""
    defaults = {}
    for ln in lines:
        m = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)="\$\{\1:-([^}]*)\}"', ln)
        if m:
            defaults[m.group(1)] = m.group(2)
    return defaults


def _resolve_shell_vars(s, defaults):
    """Resolve ``${VAR}`` / ``${VAR:-inline}`` references using ``defaults`` (env
    UNSET). Only ``${...}`` forms are touched; ``$version`` / ``$(uname -m)`` and
    other non-brace expansions are left as-is so the resolved line is byte-for-
    byte identical to the pre-fix hardcoded line."""
    def repl(m):
        var, inline = m.group(1), m.group(2)
        if var in defaults:
            return defaults[var]
        if inline is not None:
            return inline
        return m.group(0)
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", repl, s)


def resolve_publish_targets():
    """Parse ``publish.sh`` and return a structural view of its S3 targets with
    the ``ARTIFACT_BUCKET`` / ``DOCS_BUCKET`` env vars UNSET.

    On the fixed tree the buckets are parameterized as
    ``ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-panorama-sdk-v2-artifacts}"`` and the
    upload/sync lines reference ``s3://${ARTIFACT_BUCKET}/...``. This helper
    resolves ``${VAR}`` to its ``:-default`` (the env-unset case) so the returned
    ``cp_lines`` / ``sync_lines`` are byte-for-byte identical to the pre-fix
    hardcoded-literal lines — the F(X) = F'(X) preservation proof. It also works
    on the pre-fix tree, where there are no env-var assignments and the literals
    already appear inline.

    Returns a dict with the resolved bucket literals, the four ``aws s3 cp``
    upload lines (versioned + ``latest`` for ``.deb`` / ``.whl``), the docs-sync
    line and its path, and the ``if [ -d "./sphinx" ]`` guard line. The
    ``head-bucket`` preflight lines (``aws s3api head-bucket``) are intentionally
    NOT part of ``cp_lines`` / ``sync_lines``.
    """
    text = read_repo_file(PUBLISH_REL)
    lines = [ln.rstrip("\n") for ln in text.splitlines()]

    defaults = _parse_env_defaults(lines)

    cp_lines = [
        _resolve_shell_vars(ln.strip(), defaults)
        for ln in lines if re.match(r"\s*aws s3 cp\b", ln)
    ]
    sync_lines = [
        _resolve_shell_vars(ln.strip(), defaults)
        for ln in lines if re.match(r"\s*aws s3 sync\b", ln)
    ]
    guard_lines = [ln.strip() for ln in lines if 'if [ -d "./sphinx" ]' in ln]

    # After resolving ${ARTIFACT_BUCKET}/${DOCS_BUCKET} to their defaults the
    # bucket literal appears inline in each resolved line (env-unset case).
    artifact_bucket = ARTIFACT_BUCKET_LITERAL if any(
        ARTIFACT_BUCKET_LITERAL in ln for ln in cp_lines
    ) else None
    docs_bucket = DOCS_BUCKET_LITERAL if any(
        DOCS_BUCKET_LITERAL in ln for ln in sync_lines
    ) else None

    docs_sync_line = next(
        (ln for ln in sync_lines if DOCS_BUCKET_LITERAL in ln), None
    )
    docs_path = None
    if docs_sync_line is not None:
        m = re.search(r"s3://edgeml-sdk-docs/(\S+)", docs_sync_line)
        if m:
            docs_path = m.group(1)

    return {
        "artifact_bucket": artifact_bucket,
        "docs_bucket": docs_bucket,
        "cp_lines": cp_lines,
        "sync_lines": sync_lines,
        "docs_sync_line": docs_sync_line,
        "docs_path": docs_path,
        "guard_lines": guard_lines,
        "full_text": text,
    }


# --------------------------------------------------------------------------- #
# B6 — notebook load / manifest-cell location
# --------------------------------------------------------------------------- #
def load_notebook():
    """``json.load`` the notebook and return the parsed dict (validity check)."""
    with open(os.path.join(REPO_ROOT, NOTEBOOK_REL), encoding="utf-8") as fh:
        return json.load(fh)


def find_manifest_cell(nb):
    """Return ``(index, source_str)`` of the code cell that defines
    ``update_manifest_paths`` / ``old_prefix``."""
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        if "update_manifest_paths" in src and "old_prefix" in src:
            return i, src
    raise AssertionError("segmentation-manifest cell not found in notebook")
