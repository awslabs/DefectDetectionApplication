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
"""S3 preservation baseline — ``packaging.py`` synthetic identity (Req 3.3).

Spec: security-secrets-credentials-jwt-fixes — Property 2: Preservation.

The S3 fix (task 3.1) changes ONLY the synthetic-identity email domain
(``system@edgecv.com`` -> ``system@example.com``) in the auto-triggered
``greengrass_event`` claims built by ``_trigger_component_creation``. Everything
else about the construction — the derived ``component_name``, the fixed
``component_version`` / ``friendly_name`` / ``auto_triggered`` body fields, the
``sub`` / ``cognito:username`` synthetic identity, and the invoke shape — must be
byte-for-byte identical.

The baseline runs the REAL ``_trigger_component_creation`` with a stubbed Lambda
client that captures the invoked ``Payload``, and asserts the full construction
**apart from the email value** (the email is only required to be a
``system@...`` synthetic address, which holds before AND after the fix). Task 8
re-runs this unchanged.

**Validates: Requirements 3.3**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_secrets_packaging.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import os
import types

from _preservation_support import load_module_from_path

PACKAGING_REL = "edge-cv-portal/backend/functions/packaging.py"


def _shared_utils_stub():
    """Stub the shared_utils imports (packaging.py does ``from shared_utils import
    (...)`` at module top). None of these are exercised by
    ``_trigger_component_creation``."""
    mod = types.ModuleType("shared_utils")
    for name in [
        "create_response", "get_user_from_event", "log_audit_event",
        "check_user_access", "validate_required_fields", "is_cross_account_setup",
        "get_usecase_client", "assume_usecase_role", "get_usecase",
    ]:
        setattr(mod, name, lambda *a, **k: None)
    return mod


def _make_stubs(lambda_client):
    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda *a, **k: types.SimpleNamespace())
    boto3.client = lambda *a, **k: lambda_client

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exc.ClientError = ClientError
    botocore.exceptions = exc

    yaml = types.ModuleType("yaml")
    yaml.safe_load = lambda *a, **k: {}

    return {
        "boto3": boto3,
        "botocore": botocore,
        "botocore.exceptions": exc,
        "shared_utils": _shared_utils_stub(),
        "yaml": yaml,
    }


class _CapturingLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


def _load_packaging(lambda_client):
    return load_module_from_path(
        "packaging_preservation",
        PACKAGING_REL,
        injected_modules=_make_stubs(lambda_client),
    )


def _capture_greengrass_event(training_id, training_job):
    lam = _CapturingLambda()
    saved = os.environ.get("GREENGRASS_PUBLISH_FUNCTION_NAME")
    os.environ["GREENGRASS_PUBLISH_FUNCTION_NAME"] = "greengrass-publish-fn"
    try:
        mod = _load_packaging(lam)
        # boto3.client(...) is called inside the function; ensure it returns the
        # capturing client regardless of service name.
        mod.boto3.client = lambda *a, **k: lam
        mod._trigger_component_creation(training_id, training_job)
    finally:
        if saved is None:
            os.environ.pop("GREENGRASS_PUBLISH_FUNCTION_NAME", None)
        else:
            os.environ["GREENGRASS_PUBLISH_FUNCTION_NAME"] = saved
    assert len(lam.invocations) == 1, "expected exactly one greengrass invoke"
    inv = lam.invocations[0]
    return inv, json.loads(inv["Payload"])


# --------------------------------------------------------------------------- #
# S3 — synthetic identity construction preserved apart from the email value
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.3
def test_s3_synthetic_identity_construction_preserved_apart_from_email():
    inv, event = _capture_greengrass_event(
        "train-123", {"model_name": "My Model V2"}
    )

    # Invoke shape.
    assert inv["FunctionName"] == "greengrass-publish-fn"
    assert inv["InvocationType"] == "Event"

    # Event skeleton.
    assert event["httpMethod"] == "POST"
    assert event["path"] == "/api/v1/training/train-123/publish"
    assert event["pathParameters"] == {"id": "train-123"}

    body = json.loads(event["body"])
    assert body == {
        "component_name": "model-my-model-v2",  # re.sub([^a-z0-9-]) on lowercased name
        "component_version": "1.0.0",
        "friendly_name": "My Model V2",
        "auto_triggered": True,
    }

    # Synthetic identity claims — everything EXCEPT the email value is fixed.
    claims = event["requestContext"]["authorizer"]["claims"]
    assert claims["sub"] == "system"
    assert claims["cognito:username"] == "system"
    assert set(claims.keys()) == {"sub", "email", "cognito:username"}
    # Email is a synthetic ``system@...`` address (system@edgecv.com before the
    # fix, system@example.com after) — the DOMAIN is the only thing S3 changes.
    assert claims["email"].startswith("system@")


# Validates: Requirements 3.3
def test_s3_component_name_slugified_from_model_name():
    """The component name derivation (lowercase + non-alnum -> ``-``) is
    preserved for a name with spaces / punctuation."""
    _inv, event = _capture_greengrass_event(
        "t-9", {"model_name": "Widget Detector (Beta)!"}
    )
    body = json.loads(event["body"])
    assert body["component_name"] == "model-widget-detector--beta--"
    assert body["friendly_name"] == "Widget Detector (Beta)!"


# Validates: Requirements 3.3
def test_s3_default_model_name_when_absent():
    """When the training job has no ``model_name`` the default ``model`` slug is
    used — unchanged by the fix."""
    _inv, event = _capture_greengrass_event("t-1", {})
    body = json.loads(event["body"])
    assert body["component_name"] == "model-model"
    assert body["friendly_name"] == "model"
