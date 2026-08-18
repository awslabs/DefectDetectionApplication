"""Host integration tests for the portal half of
jp6-vllm-kv-cache-oom-regression (task 4.8, design "Integration Tests").

End to end under moto, through the REAL handlers — register
(`model_import.register_vllm_model` via the router), update engine
configuration (`model_import.update_vllm_engine_configuration`), package
(`packaging.package_components` -> `package_vllm_component` ->
`generate_vllm_repository`, with the artifact ZIP uploaded to moto S3),
publish (`greengrass_publish.publish_component` against the FakeGreengrass
harness) — asserting:

- the authored ``limit_mm_per_prompt`` reaches the generated ``model.json``
  VERBATIM: inspected as JSON inside the actual uploaded component ZIP and
  compared byte-identical against ``generate_vllm_repository``'s emitted
  content (2.4, 3.3);
- a JP6-infeasible configuration (feasible on JP7) is refused at publish
  with HTTP 422 and the per-architecture findings, the failing architecture
  named in the error text (2.8), with no component registration and the
  record untouched (3.1);
- the same configuration publishes with status ``overridden`` and the
  ``skip_fit_check`` override recorded on the audit event (2.8, 3.1).

Only the `estimate_weights` module-attribute seam is monkeypatched (to a
deterministic estimate, so no network/S3 metadata access happens); the REAL
`evaluate_fit` computes every verdict — that arithmetic is what makes this
an integration of the sizing model, not a unit test of the gate's branches
(which tasks 4.1/4.2 own).

HONESTY GUARD (design "Honesty Guard", binding): this file proves the
portal pipeline's math, messages, decision logic and persistence under moto
only. Nothing here loads a vLLM engine, allocates GPU memory, or reproduces
Jetson unified-memory accounting — the REAL integration tier is ON HARDWARE
(**H1-H3**, task 11: the fixed component loading and serving on
`ryanorinagxdevkithomelabjp622`, the co-resident ONNX models unchanged, the
deployment surviving a refusal), and no assertion here claims any of it.

Run (from edge-cv-portal/backend, WITH conftest):
    python3 -m pytest tests/test_jp6_kv_publish_integration.py \
        -q -p no:cacheprovider

_Requirements: 2.4, 2.8, 3.1, 3.3_
"""
import importlib.util
import io
import json
import os
import re
import sys
import uuid
import zipfile
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from boto3.dynamodb.conditions import Attr

from conftest import REGION
from vllm_fit_check import (
    GIB,
    MINIMUM_KV_CACHE_BYTES,
    WeightEstimate,
    activation_allowance,
    evaluate_fit,
)

# Reuse the established moto + FakeGreengrass harness pieces (the
# test_jp6_publish_gate_per_arch.py convention). The helpers take the env
# namespace as an argument, so they bind to THIS module's tables.
from test_vllm_publish_fit_gate import (
    _PUBLISH_PATH,
    FakeGreengrass,
    audit_events,
    publish_event,
    stored_record,
)

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-jp6-kv-publish-integration"
MODELS_TABLE_NAME = "test-models-jp6-kv-publish-integration"
USECASE_BUCKET = "test-jp6-kv-publish-integration-bucket"

_PACKAGING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "packaging.py")

HF_MODEL_ID = "example/integration-llm"


def _load_module(path, name):
    """Load a functions/ module under a distinct module name inside the
    moto mock, so its module-level boto3 clients and table names bind to
    this module's test stack (the established packaging-import pattern —
    `packaging.py` collides with the PyPI `packaging` distribution)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Environment: one training-jobs table shared by register/update, package
# and publish — the point of the end-to-end chain
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ienv(aws_stack):
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    mp.setenv("MODELS_TABLE", MODELS_TABLE_NAME)

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MODELS_TABLE_NAME,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "model_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    # The Use_Case bucket the REAL package_vllm_component uploads the
    # component ZIP into (moto S3).
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=USECASE_BUCKET)

    # Fresh model_import bound to this stack (its module-level dynamodb
    # resource and TRAINING_JOBS_TABLE bind at import time).
    sys.modules.pop("model_import", None)
    import model_import

    packaging = _load_module(
        _PACKAGING_PATH, "portal_packaging_jp6_kv_publish_integration")
    publish = _load_module(
        _PUBLISH_PATH, "portal_greengrass_publish_jp6_kv_publish_integration")

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        model_import=model_import,
        packaging=packaging,
        publish=publish,
        s3=boto3.client("s3", region_name=REGION),
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        models=resource.Table(MODELS_TABLE_NAME),
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
        audit_log=aws_stack.tables.audit_log,
    )
    sys.modules.pop("model_import", None)
    sys.modules.pop("portal_packaging_jp6_kv_publish_integration", None)
    sys.modules.pop("portal_greengrass_publish_jp6_kv_publish_integration",
                    None)
    mp.undo()


@pytest.fixture
def seeded(ienv, monkeypatch):
    """Fresh Use_Case + DataScientist; no 2s polling sleeps; fake GG for
    the publish leg (moto has no greengrassv2). Packaging's S3 client is
    NOT faked: the seeded usecase is single-account, so get_usecase_client
    returns a plain boto3 client that moto intercepts."""
    monkeypatch.setattr(ienv.publish.time, "sleep", lambda s: None)
    gg = FakeGreengrass()
    monkeypatch.setattr(ienv.publish, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    usecase_id = f"uc-{uuid.uuid4()}"
    ienv.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "JP6 KV Publish Integration Use Case",
        "account_id": "123456789012",
        "s3_bucket": USECASE_BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    ienv.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id, gg=gg)


def patch_estimates(ienv, monkeypatch, estimate):
    """Monkeypatch ONLY the estimation seam (both consumers) to a
    deterministic estimate; the REAL evaluate_fit computes every verdict."""
    def fake_estimate_weights(record, s3_head=None, hf_fetch=None):
        return estimate

    monkeypatch.setattr(ienv.model_import, "estimate_weights",
                        fake_estimate_weights)
    monkeypatch.setattr(ienv.publish, "estimate_weights",
                        fake_estimate_weights)


# ---------------------------------------------------------------------------
# Synthetic API Gateway events for the real handlers
# ---------------------------------------------------------------------------

def _claims(user_id):
    return {
        "authorizer": {
            "claims": {
                "sub": user_id,
                "email": f"{user_id}@example.com",
                "cognito:username": user_id,
            }
        }
    }


def register_event(seeded, model_name, engine_configuration):
    return {
        "httpMethod": "POST",
        "path": "/api/v1/models/vllm",
        "pathParameters": None,
        "body": json.dumps({
            "usecase_id": seeded.usecase_id,
            "model_name": model_name,
            "model_version": "1.0",
            "huggingface_model_id": HF_MODEL_ID,
            "engine_configuration": engine_configuration,
        }),
        "requestContext": _claims(seeded.user_id),
    }


def update_event(training_id, seeded, supplied):
    return {
        "httpMethod": "PUT",
        "path": f"/api/v1/models/vllm/{training_id}/engine-configuration",
        "pathParameters": {"training_id": training_id},
        "body": json.dumps({"engine_configuration": supplied}),
        "requestContext": _claims(seeded.user_id),
    }


def package_event(training_id, seeded):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/package",
        "pathParameters": {"id": training_id},
        "body": json.dumps({}),
        "requestContext": _claims(seeded.user_id),
    }


def read_model_json_from_artifact(ienv, component_package_s3):
    """The staged `model.json` text, read out of the ACTUAL component ZIP
    the packaging step uploaded to (moto) S3."""
    parsed = urlparse(component_package_s3)
    body = ienv.s3.get_object(
        Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"].read()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = [n for n in archive.namelist()
                 if n.endswith("/1/model.json")]
        assert len(names) == 1, archive.namelist()
        return names[0], archive.read(names[0]).decode("utf-8")


# ---------------------------------------------------------------------------
# Deterministic estimates, with the REAL evaluate_fit as the premise oracle
# ---------------------------------------------------------------------------

# 2 GiB of weights at util 0.5 with 2 images per prompt fits BOTH profiled
# architectures under the corrected model:
#   required = 2 + max(2, 0.75x2) x 2 + 1 = 7.00 GiB
#   arm64_jp6 budget 0.5 x 30 = 15.00 GiB, cap 0.80; arm64_jp7 budget 60.00.
FEASIBLE_ESTIMATE = WeightEstimate(
    total_bytes=2 * GIB,
    method="safetensors_files",
    detail="synthetic 2 GiB estimate (fits everywhere, integration test)",
)

# 6 GiB of weights at util 0.3 with 2 images per prompt is JP6-infeasible /
# JP7-feasible under the corrected model:
#   required = 6 + max(2, 0.75x6) x 2 + 1 = 16.00 GiB
#   arm64_jp6 budget 0.3 x 30 = 9.00 GiB (FAILS); arm64_jp7 budget 36.00.
INFEASIBLE_ESTIMATE = WeightEstimate(
    total_bytes=6 * GIB,
    method="safetensors_files",
    detail="synthetic 6 GiB estimate (JP6-infeasible, JP7-feasible)",
)

FEASIBLE_CONFIG = {"gpu_memory_utilization": 0.5, "max_model_len": 4096}
INFEASIBLE_CONFIG = {"gpu_memory_utilization": 0.3, "max_model_len": 4096,
                     "limit_mm_per_prompt": {"image": 2}}


def assert_feasible_premise():
    """Guard the fixture's premise against the SHIPPED sizing model, so a
    constant change surfaces here instead of silently voiding the case."""
    findings = evaluate_fit(
        {"gpu_memory_utilization": "0.5", "limit_mm_per_prompt": {"image": 2}},
        FEASIBLE_ESTIMATE, ["arm64_jp6", "arm64_jp7"])
    premise = {f.arch: f.fits for f in findings}
    assert premise == {"arm64_jp6": True, "arm64_jp7": True}, premise


def assert_infeasible_premise():
    findings = evaluate_fit(
        {"gpu_memory_utilization": "0.3", "limit_mm_per_prompt": {"image": 2}},
        INFEASIBLE_ESTIMATE, ["arm64_jp6", "arm64_jp7"])
    premise = {f.arch: f.fits for f in findings}
    assert premise == {"arm64_jp6": False, "arm64_jp7": True}, premise


def register_and_package(ienv, seeded, model_name, engine_configuration):
    """The shared register -> package legs (both scenarios), through the
    REAL handlers. Returns (training_id, package_response_body)."""
    response = ienv.model_import.handler(
        register_event(seeded, model_name, engine_configuration), None)
    assert response["statusCode"] == 201, response["body"]
    training_id = json.loads(response["body"])["training_id"]

    response = ienv.packaging.package_components(
        package_event(training_id, seeded), None)
    assert response["statusCode"] == 200, response["body"]
    return training_id, json.loads(response["body"])


# ---------------------------------------------------------------------------
# Register -> update -> package -> publish: the authored multimodal limit
# reaches model.json VERBATIM and the publish passes (2.4, 3.3, 3.1)
# ---------------------------------------------------------------------------

def test_authored_multimodal_limit_reaches_model_json_verbatim_end_to_end(
        ienv, seeded, monkeypatch):
    assert_feasible_premise()
    patch_estimates(ienv, monkeypatch, FEASIBLE_ESTIMATE)

    # Register with the five pre-existing settings authored; the multimodal
    # limit arrives via the UPDATE leg below, so both write paths are on
    # the wire to model.json.
    response = ienv.model_import.handler(
        register_event(seeded, "Integration Fit LLM", FEASIBLE_CONFIG), None)
    assert response["statusCode"] == 201, response["body"]
    body = json.loads(response["body"])
    training_id = body["training_id"]
    assert body["publish_eligible"] is True
    assert body["fit_check"]["status"] == "passed", body["fit_check"]

    # Update the engine configuration: author the two-image limit.
    response = ienv.model_import.handler(
        update_event(training_id, seeded,
                     {"limit_mm_per_prompt": {"image": 2}}), None)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])
    assert body["engine_configuration"]["limit_mm_per_prompt"] == {
        "image": 2}
    # The REAL evaluate_fit sized the AUTHORED value (2.4): every finding
    # of the update's non-blocking fit check carries images_per_prompt 2.
    assert body["fit_check"]["status"] == "passed", body["fit_check"]
    assert all(f["images_per_prompt"] == 2
               for f in body["fit_check"]["findings"])

    # Package: the REAL vLLM bypass generates the repository, zips it and
    # uploads the artifact to (moto) S3, then records packaged_components.
    response = ienv.packaging.package_components(
        package_event(training_id, seeded), None)
    assert response["statusCode"] == 200, response["body"]
    packaged = json.loads(response["body"])["packaged_components"]
    assert [c["target"] for c in packaged] == [
        "jetson-xavier-jp6", "jetson-xavier-jp7"]
    assert all(c["status"] == "packaged" for c in packaged)
    assert all(c["supported_architectures"] == ["arm64_jp6", "arm64_jp7"]
               for c in packaged)

    # The authored limit reached the generated model.json VERBATIM —
    # inspected as JSON inside the ACTUAL uploaded ZIP (2.4, 3.3).
    path, model_json_text = read_model_json_from_artifact(
        ienv, packaged[0]["component_package_s3"])
    assert path == "integration-fit-llm/1/model.json"
    model_json = json.loads(model_json_text)
    assert model_json["limit_mm_per_prompt"] == {"image": 2}
    staged_images = model_json["limit_mm_per_prompt"]["image"]
    assert isinstance(staged_images, int) and \
        not isinstance(staged_images, bool)
    # The complete resolved configuration, verbatim, plus the documented
    # `model` reference — and nothing else (3.3).
    assert model_json == {
        "dtype": "auto",
        "gpu_memory_utilization": 0.5,
        "max_model_len": 4096,
        "tensor_parallel_size": 1,
        "enforce_eager": True,
        "limit_mm_per_prompt": {"image": 2},
        "model": HF_MODEL_ID,
    }
    # ...and the ZIP's bytes are exactly generate_vllm_repository's output
    # for the stored record (the packer changed nothing in flight).
    stored = stored_record(ienv, training_id)
    generated = ienv.packaging.generate_vllm_repository(stored)
    assert model_json_text == generated[path]

    # Publish: the REAL evaluate_fit passes both architectures.
    response = ienv.publish.publish_component(
        publish_event(training_id, seeded.user_id), None)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])
    assert body["fit_check"]["status"] == "passed", body["fit_check"]
    by_arch = {f["arch"]: f for f in body["fit_check"]["findings"]}
    assert set(by_arch) == {"arm64_jp6", "arm64_jp7"}
    assert all(f["fits"] is True for f in by_arch.values())
    # The verdict was computed by the REAL corrected arithmetic, from the
    # authored two-image limit (2.4).
    expected_required = (FEASIBLE_ESTIMATE.total_bytes
                         + activation_allowance(
                             FEASIBLE_ESTIMATE.total_bytes, 2)
                         + MINIMUM_KV_CACHE_BYTES)
    for finding in by_arch.values():
        assert finding["images_per_prompt"] == 2
        assert finding["required_bytes"] == expected_required

    # One Per_JetPack_Component per packaged target actually registered.
    assert [r["ComponentName"] for r in seeded.gg.created] == [
        "model-vllm-integration-fit-llm-jetson-xavier-jp6",
        "model-vllm-integration-fit-llm-jetson-xavier-jp7",
    ]
    stored = stored_record(ienv, training_id)
    assert stored["published"] is True
    assert stored["published_component"]["component_name"] == \
        "model-vllm-integration-fit-llm"

    # The whole chain is audited: one event per leg for this user.
    actions = [e["action"] for e in audit_events(ienv, seeded.user_id)]
    for action in ("register_vllm_model",
                   "update_vllm_engine_configuration",
                   "package_components",
                   "publish_greengrass_component"):
        assert actions.count(action) == 1, actions


# ---------------------------------------------------------------------------
# JP6-infeasible / JP7-feasible: refused at publish with the per-arch
# findings (2.8), record untouched (3.1)
# ---------------------------------------------------------------------------

def test_jp6_infeasible_configuration_is_refused_at_publish_with_findings(
        ienv, seeded, monkeypatch):
    assert_infeasible_premise()
    patch_estimates(ienv, monkeypatch, INFEASIBLE_ESTIMATE)

    training_id, _ = register_and_package(
        ienv, seeded, "Integration Doomed LLM", INFEASIBLE_CONFIG)
    before = stored_record(ienv, training_id)

    response = ienv.publish.publish_component(
        publish_event(training_id, seeded.user_id), None)

    assert response["statusCode"] == 422, response["body"]
    body = json.loads(response["body"])

    # 'failed' with ALL per-architecture findings carried, the passing one
    # included, computed by the REAL evaluate_fit (2.8).
    assert body["fit_check"]["status"] == "failed"
    assert {f["arch"]: f["fits"] for f in body["fit_check"]["findings"]} \
        == {"arm64_jp6": False, "arm64_jp7": True}
    assert body["fit_check"]["estimate"]["total_bytes"] == 6 * GIB

    # Only the FAILING architecture is named in the error text, and the
    # never-lower invariant holds on the real message too.
    assert "arm64_jp6" in body["error"]
    assert "arm64_jp7" not in body["error"]
    assert not re.search(
        r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization",
        body["error"], re.IGNORECASE), body["error"]

    # No component registration; the record kept its post-package state.
    assert seeded.gg.created == []
    after = stored_record(ienv, training_id)
    assert "published" not in after
    assert "published_component" not in after
    assert after["updated_at"] == before["updated_at"]
    assert after["packaged_components"] == before["packaged_components"]


# ---------------------------------------------------------------------------
# The same configuration publishes with `overridden` + audit under
# skip_fit_check (2.8, 3.1)
# ---------------------------------------------------------------------------

def test_jp6_infeasible_configuration_publishes_overridden_with_skip(
        ienv, seeded, monkeypatch):
    assert_infeasible_premise()
    patch_estimates(ienv, monkeypatch, INFEASIBLE_ESTIMATE)

    training_id, _ = register_and_package(
        ienv, seeded, "Integration Override LLM", INFEASIBLE_CONFIG)

    response = ienv.publish.publish_component(
        publish_event(training_id, seeded.user_id,
                      extra_body={"skip_fit_check": True}), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    # Status 'overridden' with the real findings retained and the failing
    # architecture named in the annotation.
    assert body["fit_check"]["status"] == "overridden"
    assert {f["arch"]: f["fits"] for f in body["fit_check"]["findings"]} \
        == {"arm64_jp6": False, "arm64_jp7": True}
    assert "arm64_jp6" in body["fit_check"]["message"]
    assert "skip_fit_check" in body["fit_check"]["message"]

    # The publish actually proceeded through component registration.
    assert [r["ComponentName"] for r in seeded.gg.created] == [
        "model-vllm-integration-override-llm-jetson-xavier-jp6",
        "model-vllm-integration-override-llm-jetson-xavier-jp7",
    ]
    assert stored_record(ienv, training_id)["published"] is True

    # The override is recorded on the audit event.
    events = [e for e in audit_events(ienv, seeded.user_id)
              if e["action"] == "publish_greengrass_component"]
    assert len(events) == 1
    assert events[0]["result"] == "success"
    assert events[0]["details"]["skip_fit_check"] is True
