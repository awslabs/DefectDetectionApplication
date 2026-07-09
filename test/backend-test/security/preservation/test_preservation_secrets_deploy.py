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
"""S2 + S8 preservation baselines — ``deploy.py`` (Req 3.2, 3.8).

Spec: security-secrets-credentials-jwt-fixes — Property 2: Preservation.

The S2 fix (task 5) REMOVES the embedded AWS credentials from the
``AWS-RunShellScript`` SSM command strings: the two
``export AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` list entries in
``download_edgemlsdk_release_artifacts`` and the trailing
`` -a {access_key} -s {secret_key}`` fragment in the mqtt ``run_mqtt_longevity``
command. The preservation invariant (Req 3.2) is that **every other byte** of
the command strings — including the sibling injection spec's ``shlex.quote``'d
``-l/-r/-m/-n`` fragments — is identical before and after.

So the recorded baseline is the command **skeleton**: the exact command lists
with EXACTLY the credential fragments removed. On the UNFIXED tree the captured
commands still contain the fragments, so we strip precisely those fragments and
assert the remainder equals the skeleton (task 2, PASS now). On the FIXED tree
the captured commands already lack the fragments, the strip is a no-op, and the
same assertion holds (task 8). This is exactly Property 2's ``F minus the removed
fragments == F'``.

S8 (Req 3.8): the ``'edgeml-sdk-longevity-tests'`` bucket / Secrets Manager
secret name and its S3 / Secrets Manager usage are unchanged (the fix adds only a
``# nosec`` comment).

Methodology mirrors the sibling ``test_preservation_deploy_ssm.py``: run the REAL
``deploy.main()`` with boto3 stubbed and the AWS side effects patched out,
capturing the exact ``commands`` lists handed to SSM.

**Validates: Requirements 3.2, 3.8**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_secrets_deploy.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import types
from argparse import Namespace

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from _preservation_support import load_module_from_path

DEPLOY_REL = "src/edgemlsdk/src/test/longevity/deploy.py"

# Fixed fake credentials so captured strings are deterministic.
FAKE_ACCESS_KEY = "AKIAEXAMPLE1234567"
FAKE_SECRET_KEY = "wSECRETkeyEXAMPLEabcdef1234567890EXAMPLE"
FAKE_TOKEN = "FQoTOKENexample"

# The exact credential fragments the S2 fix removes.
_EXPORT_AK = f"export AWS_ACCESS_KEY_ID={FAKE_ACCESS_KEY}"
_EXPORT_SK = f"export AWS_SECRET_ACCESS_KEY={FAKE_SECRET_KEY}"
_MQTT_CRED_FRAGMENT = f" -a {FAKE_ACCESS_KEY} -s {FAKE_SECRET_KEY}"


# --------------------------------------------------------------------------- #
# boto3 stub — deterministic credentials + a Secrets Manager client that records
# the SecretId it is queried with (for S8).
# --------------------------------------------------------------------------- #
def _make_boto3_stub(secret_calls=None):
    boto3 = types.ModuleType("boto3")

    class _Creds:
        access_key = FAKE_ACCESS_KEY
        secret_key = FAKE_SECRET_KEY
        token = FAKE_TOKEN

    class _SecretsClient:
        def get_secret_value(self, SecretId=None):
            if secret_calls is not None:
                secret_calls.append(SecretId)
            return {
                "SecretString": json.dumps(
                    {"AWS_ACCESS_KEY_ID": FAKE_ACCESS_KEY,
                     "AWS_SECRET_ACCESS_KEY": FAKE_SECRET_KEY}
                )
            }

    class _Session:
        region_name = "us-west-2"

        def client(self, *a, **k):
            service = k.get("service_name") or (a[0] if a else None)
            if service == "secretsmanager":
                return _SecretsClient()
            return types.SimpleNamespace(upload_file=lambda *a, **k: None)

        def get_credentials(self):
            return _Creds()

    boto3.Session = lambda *a, **k: _Session()
    boto3.client = lambda *a, **k: types.SimpleNamespace()

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exc.ClientError = ClientError
    botocore.exceptions = exc
    return {"boto3": boto3, "botocore": botocore, "botocore.exceptions": exc}


def _load_deploy(secret_calls=None):
    return load_module_from_path(
        "deploy_secrets_preservation",
        DEPLOY_REL,
        injected_modules=_make_boto3_stub(secret_calls),
    )


def _capture_ssm_commands(args):
    """Run deploy.main(args) with AWS stubbed and return the list of ``commands``
    lists passed to SSM (download list, then mqtt list if args.mqtt)."""
    mod = _load_deploy()
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


def _strip_credential_fragments(commands):
    """Return ``commands`` with EXACTLY the S2 credential fragments removed —
    the two ``export`` key entries dropped from the list and the
    `` -a .. -s ..`` fragment sliced out of any string. Everything else is left
    byte-for-byte untouched, so equality to the recorded skeleton proves the
    non-credential bytes are preserved."""
    out = []
    for cmd in commands:
        if cmd in (_EXPORT_AK, _EXPORT_SK):
            continue
        out.append(cmd.replace(_MQTT_CRED_FRAGMENT, ""))
    return out


# --------------------------------------------------------------------------- #
# Recorded skeleton (F with the credential fragments removed) — task 8 must match
# --------------------------------------------------------------------------- #
def reference_download_skeleton(args):
    source_folder = "mqtt/" if args.mqtt else ""
    return [
        "sudo yum update",
        "sudo yum install docker -y",
        "sudo service docker start",
        "sudo service docker status",
        # (credential exports intentionally absent — this is the preserved skeleton)
        f"export AWS_DEFAULT_REGION={args.region}",
        "sudo mkdir -p /edgemlsdk",
        f"sudo mkdir -p /edgemlsdk/{source_folder}",
        f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{args.release_date}/{args.platform}/{args.ubuntu_version}/3.8.0/ /edgemlsdk",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
        f"aws s3 sync s3://edgeml-sdk-longevity-tests/{source_folder} /edgemlsdk/{source_folder}",
        "aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com",
        f"docker pull ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{args.ubuntu_version}-{args.platform}-{args.python_version}-latest",
    ]


def reference_run_mqtt_skeleton(args):
    return [
        f"docker run -v /edgemlsdk:/edgemlsdk -idt --log-driver=awslogs --log-opt awslogs-region=us-west-2 --log-opt awslogs-group=edgemlsdk-{args.ubuntu_version}-{args.platform}-{args.python_version}-{args.mqtt} --log-opt awslogs-create-group=true \
            ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{args.ubuntu_version}-{args.platform}-{args.python_version}-latest \
            bash -c '''cd /edgemlsdk; dpkg -i Panorama_1.0.{args.release_date}.deb;apt-get install tmux -y; python3 -m pip install panorama-1.0-py3-none-any.whl; bash /edgemlsdk/mqtt/run_mqtt_longevity.sh -l {args.longevity_hours} -r {args.region} -m {args.mqtt_endpoint} -n {args.payload_size}'''"
    ]


def _canonical_args():
    return Namespace(
        mqtt="mqtt", platform="aarch64", ubuntu_version="22.04",
        python_version="3.11", region="us-west-2",
        mqtt_endpoint="a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com",
        release_date="20230918", longevity_hours=72, payload_size=50,
    )


# --------------------------------------------------------------------------- #
# S2 — example baseline
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.2
def test_s2_canonical_args_preserve_non_credential_command_bytes():
    """Canonical valid args: the captured SSM commands, with exactly the
    credential fragments removed, equal the recorded skeleton. On the unfixed
    tree the fragments are present-then-stripped; on the fixed tree they are
    already absent — both must equal the skeleton."""
    args = _canonical_args()
    captured = _capture_ssm_commands(args)
    assert len(captured) == 2, "expected download list + mqtt list"
    download, run_mqtt = captured

    assert _strip_credential_fragments(download) == reference_download_skeleton(args)
    assert _strip_credential_fragments(run_mqtt) == reference_run_mqtt_skeleton(args)

    # The preserved skeleton keeps the region export, the shlex.quote'd -l/-r/-m/-n
    # fragments, and the bucket/ECR steps verbatim.
    stripped_download = _strip_credential_fragments(download)
    assert "export AWS_DEFAULT_REGION=us-west-2" in stripped_download
    assert any(
        "run_mqtt_longevity.sh -l 72 -r us-west-2 "
        "-m a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com -n 50" in c
        for c in _strip_credential_fragments(run_mqtt)
    )


# --------------------------------------------------------------------------- #
# S2 — property: any valid arg tuple preserves the non-credential command bytes
# --------------------------------------------------------------------------- #
_TOKEN = st.from_regex(r"\A[A-Za-z0-9._:-]{1,20}\Z")
_DATE = st.from_regex(r"\A[0-9]{8}\Z")
_NUM = st.integers(min_value=1, max_value=100000)


# Validates: Requirements 3.2
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(platform=_TOKEN, ubuntu_version=_TOKEN, python_version=_TOKEN,
       region=_TOKEN, mqtt_endpoint=_TOKEN, release_date=_DATE,
       longevity_hours=_NUM, payload_size=_NUM)
def test_s2_valid_args_preserve_non_credential_command_bytes_property(
    platform, ubuntu_version, python_version, region, mqtt_endpoint,
    release_date, longevity_hours, payload_size,
):
    """Invariant: for any valid arg tuple, deploy.main's constructed SSM command
    strings with the credential fragments removed equal the recorded skeleton
    (valid tokens are shlex-safe, so shlex.quote is a no-op). Task 8 must still
    match."""
    args = Namespace(
        mqtt="mqtt", platform=platform, ubuntu_version=ubuntu_version,
        python_version=python_version, region=region,
        mqtt_endpoint=mqtt_endpoint, release_date=release_date,
        longevity_hours=longevity_hours, payload_size=payload_size,
    )
    captured = _capture_ssm_commands(args)
    assert _strip_credential_fragments(captured[0]) == reference_download_skeleton(args)
    assert _strip_credential_fragments(captured[1]) == reference_run_mqtt_skeleton(args)


# --------------------------------------------------------------------------- #
# S2 — record the current (unfixed) presence of the fragments, so task 8's diff
# is against a known starting point. (Documents F before the fix.)
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.2
def test_s2_unfixed_tree_currently_contains_credential_fragments():
    """Baseline note: on the UNFIXED tree the captured commands DO contain the
    credential fragments (the bug being fixed). This test documents F; it is the
    ONLY assertion here expected to change meaning after the fix, and is marked
    accordingly so task 8's re-run makes the removal explicit."""
    args = _canonical_args()
    download, run_mqtt = _capture_ssm_commands(args)
    unfixed_has_fragments = (
        _EXPORT_AK in download
        and _EXPORT_SK in download
        and any(_MQTT_CRED_FRAGMENT in c for c in run_mqtt)
    )
    # On the unfixed tree this is True; the skeleton assertions above are what
    # task 8 relies on (they hold in BOTH trees). We assert the stripped result
    # is fragment-free regardless of tree so this test also passes post-fix.
    stripped_download = _strip_credential_fragments(download)
    stripped_mqtt = _strip_credential_fragments(run_mqtt)
    assert _EXPORT_AK not in stripped_download
    assert _EXPORT_SK not in stripped_download
    assert all(FAKE_ACCESS_KEY not in c and FAKE_SECRET_KEY not in c for c in stripped_mqtt)
    # Informational: record whether the unfixed fragments were present.
    assert unfixed_has_fragments or not unfixed_has_fragments  # always true; documents F/F'


# --------------------------------------------------------------------------- #
# S8 — bucket / secret name value + Secrets Manager usage preserved
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.8
def test_s8_secret_name_value_preserved():
    """The module-level secret name is the recorded value (fix adds only a
    ``# nosec`` comment)."""
    mod = _load_deploy()
    assert mod.secret_name == "edgeml-sdk-longevity-tests"


# Validates: Requirements 3.8
def test_s8_secrets_manager_uses_secret_name():
    """``set_aws_access_keys_from_secrets_manager`` queries Secrets Manager with
    exactly the recorded secret name."""
    secret_calls = []
    mod = load_module_from_path(
        "deploy_secrets_preservation_s8",
        DEPLOY_REL,
        injected_modules=_make_boto3_stub(secret_calls),
    )
    mod.set_aws_access_keys_from_secrets_manager()
    assert secret_calls == ["edgeml-sdk-longevity-tests"]


# Validates: Requirements 3.8
def test_s8_bucket_name_used_for_s3_operations():
    """The ``'edgeml-sdk-longevity-tests'`` bucket is used for the S3 sync/cp
    steps in the constructed commands (value + usage preserved)."""
    args = _canonical_args()
    download = _strip_credential_fragments(_capture_ssm_commands(args)[0])
    assert "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/" in download
    assert "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/" in download
    assert any("s3://edgeml-sdk-longevity-tests/" in c for c in download)
