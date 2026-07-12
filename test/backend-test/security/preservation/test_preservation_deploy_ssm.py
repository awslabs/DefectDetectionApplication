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
"""#2 deploy.py SSM-command preservation baseline (Req 3.2).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

``deploy.py`` builds two ``AWS-RunShellScript`` command lists (the release-
artifact download and the mqtt longevity run) by f-string-interpolating the
argparse args. The fix (task 4) allowlist-validates each arg and wraps every
interpolated value in ``shlex.quote``. Because ``shlex.quote`` is a NO-OP on
clean tokens (``aarch64``, ``22.04``, ``3.11``, ``us-west-2``, an 8-digit date,
numeric sizes), the constructed command strings for LEGITIMATE args must be
identical before and after the fix.

NOTE (re-pinned by security-secrets-credentials-jwt-fixes S2 / Req 2.2, task 5):
that sibling spec REMOVED the two ``export AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` entries and the trailing `` -a {access_key} -s
{secret_key}`` mqtt fragment from the constructed commands. The reference
templates below have therefore been re-pinned to the post-S2 shape; every other
byte of the injection-spec baseline is preserved.

Methodology: run the REAL ``deploy.py`` ``main()`` with boto3 stubbed and the AWS
side effects patched out, capturing the exact ``commands`` lists handed to SSM.
The baseline is the ``reference_*`` template (the verbatim f-string shape); the
invariant is ``main()``'s captured commands == template for all valid args, so
task 13 re-runs this unchanged against the fixed source.

**Validates: Requirements 3.2**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_deploy_ssm.py \
        -p no:cacheprovider --noconftest -v
"""
import sys
import types
from argparse import Namespace

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from _preservation_support import load_module_from_path

# Fixed fake credentials so captured strings are deterministic (these mirror the
# embedded-credential lines whose out-of-scope preservation is guarded in
# test_preservation_out_of_scope_guard.py — Req 3.7).
FAKE_ACCESS_KEY = "AKIAEXAMPLE1234567"
FAKE_SECRET_KEY = "wSECRETkeyEXAMPLEabcdef1234567890EXAMPLE"
FAKE_TOKEN = "FQoTOKENexample"


def _make_boto3_stub():
    boto3 = types.ModuleType("boto3")

    class _Creds:
        access_key = FAKE_ACCESS_KEY
        secret_key = FAKE_SECRET_KEY
        token = FAKE_TOKEN

    class _Session:
        region_name = "us-west-2"

        def client(self, *a, **k):
            return types.SimpleNamespace()

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


def _load_deploy():
    return load_module_from_path(
        "deploy_preservation",
        "src/edgemlsdk/src/test/longevity/deploy.py",
        injected_modules=_make_boto3_stub(),
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


# --------------------------------------------------------------------------- #
# Reference model — the verbatim F command templates (the recorded baseline).
# shlex.quote is a no-op on clean tokens, so F' must reproduce these exactly for
# valid args.
# --------------------------------------------------------------------------- #
def reference_download(args, access_key, secret_key):
    # NOTE: the two ``export AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``
    # entries were removed by security-secrets-credentials-jwt-fixes S2/Req 2.2
    # (task 5); the ``access_key``/``secret_key`` params are kept in the
    # signature for backward compatibility with existing callers/tests but are
    # no longer interpolated.
    source_folder = "mqtt/" if args.mqtt else ""
    return [
        "sudo yum update",
        "sudo yum install docker -y",
        "sudo service docker start",
        "sudo service docker status",
        f"export AWS_DEFAULT_REGION={args.region}",
        "sudo mkdir -p /edgemlsdk",
        f"sudo mkdir -p /edgemlsdk/{source_folder}",
        # NOTE (re-pinned by security-s3-bucket-squatting-fixes B1): the S3
        # bucket-squatting batch PREPENDED two ``aws s3api head-bucket
        # --expected-bucket-owner`` preflight entries (one before the panorama
        # artifact sync, one before the longevity accesses). They carry no
        # credentials and leave every existing entry byte-for-byte identical.
        f"aws s3api head-bucket --bucket panorama-sdk-v2-artifacts --expected-bucket-owner {args.artifacts_bucket_owner}",
        f"aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.{args.release_date}/{args.platform}/{args.ubuntu_version}/3.8.0/ /edgemlsdk",
        f"aws s3api head-bucket --bucket edgeml-sdk-longevity-tests --expected-bucket-owner {args.longevity_bucket_owner}",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/",
        "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
        f"aws s3 sync s3://edgeml-sdk-longevity-tests/{source_folder} /edgemlsdk/{source_folder}",
        "aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com",
        f"docker pull ACCOUNT_ID.dkr.ecr.us-west-2.amazonaws.com/edgemlsdk:{args.ubuntu_version}-{args.platform}-{args.python_version}-latest",
    ]


def reference_run_mqtt(args, access_key, secret_key):
    # NOTE: the trailing `` -a {access_key} -s {secret_key}`` fragment was
    # removed by security-secrets-credentials-jwt-fixes S2/Req 2.2 (task 5);
    # the ``access_key``/``secret_key`` params are kept in the signature for
    # backward compatibility with existing callers/tests but are no longer
    # interpolated.
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
        # Owner args added by the S3 bucket-squatting batch (B1). Explicit
        # values so head-bucket owner resolution short-circuits (the stubbed
        # boto3 session has no real ``sts`` get_caller_identity).
        artifacts_bucket_owner="123456789012",
        longevity_bucket_owner="123456789012",
    )


# --------------------------------------------------------------------------- #
# Example baseline
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.2
def test_canonical_args_reproduce_reference_ssm_commands():
    """Canonical valid args produce exactly the recorded download + mqtt command
    lists."""
    args = _canonical_args()
    captured = _capture_ssm_commands(args)

    assert len(captured) == 2, "expected download list + mqtt list"
    download, run_mqtt = captured

    assert download == reference_download(args, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    assert run_mqtt == reference_run_mqtt(args, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)

    # Spot-check the concrete interpolated values survive verbatim.
    assert "aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0.20230918/aarch64/22.04/3.8.0/ /edgemlsdk" in download
    assert any("run_mqtt_longevity.sh -l 72 -r us-west-2 -m a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com -n 50" in c for c in run_mqtt)


# --------------------------------------------------------------------------- #
# Property: any valid arg tuple reproduces the reference command strings
# --------------------------------------------------------------------------- #
# Valid tokens use only shlex-safe characters (design allowlist:
# ^[A-Za-z0-9._:-]+$, release_date ^\d{8}$), so shlex.quote is a no-op and the
# fixed code must reproduce the same strings.
_TOKEN = st.from_regex(r"\A[A-Za-z0-9._:-]{1,20}\Z")
_DATE = st.from_regex(r"\A[0-9]{8}\Z")
_NUM = st.integers(min_value=1, max_value=100000)


# Validates: Requirements 3.2
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(platform=_TOKEN, ubuntu_version=_TOKEN, python_version=_TOKEN,
       region=_TOKEN, mqtt_endpoint=_TOKEN, release_date=_DATE,
       longevity_hours=_NUM, payload_size=_NUM)
def test_valid_args_preserve_ssm_command_strings_property(
    platform, ubuntu_version, python_version, region, mqtt_endpoint,
    release_date, longevity_hours, payload_size,
):
    """Invariant: for any valid arg tuple, deploy.main's constructed SSM command
    strings equal the recorded F template (task 13 must still match)."""
    args = Namespace(
        mqtt="mqtt", platform=platform, ubuntu_version=ubuntu_version,
        python_version=python_version, region=region,
        mqtt_endpoint=mqtt_endpoint, release_date=release_date,
        longevity_hours=longevity_hours, payload_size=payload_size,
        artifacts_bucket_owner="123456789012",
        longevity_bucket_owner="123456789012",
    )
    captured = _capture_ssm_commands(args)
    assert captured[0] == reference_download(args, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    assert captured[1] == reference_run_mqtt(args, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
