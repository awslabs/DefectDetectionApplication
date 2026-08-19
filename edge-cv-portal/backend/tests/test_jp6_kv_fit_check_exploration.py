"""
Bug-condition exploration, PORTAL half (spec:
jp6-vllm-kv-cache-oom-regression, task 1).

**Property 1: Bug Condition — the publish-time sizing model is unsound and
the publish gate lets a per-architecture infeasibility ship.**

Every case asserts the FIXED expected behavior, so on the UNFIXED tree all
three are EXPECTED TO FAIL — each failure is the counterexample for one
defect leg of the ryanorinagxdevkithomelabjp622 2026-08-17 incident:

- Case 1 (defect 1.1) — the incident replay. ``evaluate_fit`` for the
  staged configuration (``gpu_memory_utilization = 0.4``, ~6.5 GiB of
  weights, ``arm64_jp6``) must report ``fits = False``. Unfixed:
  ``0.4 × 30 GiB = 12.00 GiB ≥ 6.5 + 1 = 7.5 GiB`` PASSES with a claimed
  4.50 GiB of slack, while the device computed the KV remainder as
  **−7.83 GiB** (``model weights take 6.59GiB; non_torch_memory takes
  8.29GiB; PyTorch activation peak memory takes 4.93GiB``) — the formula
  omits the ~4.9 GiB activation/profiling peak entirely and knows nothing
  about the ≈5.7 GiB of co-resident ONNX Triton stubs on the same unified
  memory.
- Case 2 (defects 1.2, 1.3) — the co-tenancy hazard. ``util = 0.9`` on
  ``arm64_jp6`` claims 27 GiB of a ~29.95 GiB device on which co-tenants
  already hold ~6 GiB; it must not be reported as fitting, and no message
  may offer raising the fraction past the Fraction_Cap
  (``(30 − 6)/30 = 0.80``). Unfixed: it fits, and the remediation says
  "raise gpu_memory_utilization" with no hazard stated at all.
- Case 3 (defect 1.8) — the per-architecture escape. A configuration that
  is infeasible on ``arm64_jp6`` but feasible on ``arm64_jp7`` must be
  refused with 422. Unfixed: ``every_arch_fails = all(not finding.fits …)``
  ships it with status ``warnings`` — exactly how this configuration
  reached the fleet.

HONESTY GUARD (binding). Pure sizing math plus the moto-backed publish
handler: no vLLM engine, no GPU, no Jetson memory accounting anywhere. The
device-measured numbers appear only as recorded evidence in the assertion
messages.

Run (from ``edge-cv-portal/backend``, WITH the suite conftest):
    python3 -m pytest tests/test_jp6_kv_fit_check_exploration.py \
      -q -p no:cacheprovider

_Requirements: 1.1, 1.2, 1.3, 1.8_
"""
import importlib.util
import json
import os
import re
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION
from vllm_fit_check import (
    DEVICE_MEMORY_PROFILE_BYTES,
    GIB,
    MINIMUM_KV_CACHE_BYTES,
    WeightEstimate,
    evaluate_fit,
)

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-jp6-kv-explore"
MODELS_TABLE_NAME = "test-models-jp6-kv-explore"

_PUBLISH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "greengrass_publish.py")

# The incident's numbers (bugfix.md "The budget arithmetic, verbatim from
# the live 1.0.59 device").
INCIDENT_UTILIZATION = 0.4
INCIDENT_WEIGHTS_BYTES = int(6.5 * GIB)
MEASURED_ACTIVATION_PEAK_BYTES = int(4.92 * GIB)
MEASURED_KV_REMAINDER_BYTES = int(-7.83 * GIB)
MEASURED_CO_TENANCY_BYTES = 6 * GIB
JP6_FRACTION_CAP = (
    (DEVICE_MEMORY_PROFILE_BYTES['arm64_jp6'] - MEASURED_CO_TENANCY_BYTES)
    / DEVICE_MEMORY_PROFILE_BYTES['arm64_jp6'])


def _gib(num_bytes):
    return "{:.2f} GiB".format(num_bytes / GIB)


# ---------------------------------------------------------------------------
# Case 1 — incident replay: the fit math (defect 1.1)
# ---------------------------------------------------------------------------

def test_case1_incident_configuration_must_not_be_reported_as_fitting():
    """The exact staged configuration that could not load must be refused.

    Corrected model (design Decision 2): ``activation_allowance =
    max(2 GiB, 0.75 × weights) = 4.88 GiB``, so ``required = 6.5 + 4.88 + 1
    = 12.38 GiB`` against a ``0.4 × 30 GiB = 12.00 GiB`` budget — a
    0.38 GiB near-miss that matches the device's 0.65 GiB remainder against
    the 1 GiB floor.
    """
    findings = evaluate_fit(
        {'gpu_memory_utilization': INCIDENT_UTILIZATION},
        INCIDENT_WEIGHTS_BYTES,
        ['arm64_jp6'])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.fits is False, (
        "the shipped model reports the incident configuration as FITTING: "
        "budget {} vs required {} (claimed slack {}), while the device "
        "measured the KV remainder as {} for the same load "
        "(activation peak {}, absent from the formula entirely)".format(
            _gib(finding.budget_bytes), _gib(finding.required_bytes),
            _gib(finding.budget_bytes - finding.required_bytes),
            _gib(MEASURED_KV_REMAINDER_BYTES),
            _gib(MEASURED_ACTIVATION_PEAK_BYTES)))
    assert finding.required_bytes > INCIDENT_WEIGHTS_BYTES + \
        MINIMUM_KV_CACHE_BYTES, (
        "required_bytes ({}) is still weights + KV floor: no activation "
        "allowance is modelled".format(_gib(finding.required_bytes)))
    assert re.search(r"activation", finding.message, re.IGNORECASE), (
        "the verdict message names no activation term: {!r}".format(
            finding.message))
    assert re.search(r"estimate", finding.message, re.IGNORECASE), (
        "the activation allowance is not labelled an estimate: {!r}".format(
            finding.message))


# ---------------------------------------------------------------------------
# Case 2 — the co-tenancy hazard (defects 1.2, 1.3)
# ---------------------------------------------------------------------------

def test_case2_utilization_above_the_fraction_cap_is_not_fitting():
    """``gpu_memory_utilization`` is a fraction of TOTAL device memory. At
    0.9 on JP6 the budget is 27.00 GiB while the three co-resident ONNX
    Triton stubs alone hold ≈5.7 GiB of the ~29.95 GiB the engine sees — the
    claim overlaps memory other GPU models are using, which is how the
    current remediation ("raise gpu_memory_utilization") can convert one
    broken model into a broken vision stack.
    """
    findings = evaluate_fit(
        {'gpu_memory_utilization': 0.9},
        INCIDENT_WEIGHTS_BYTES,
        ['arm64_jp6'])
    finding = findings[0]

    assert finding.fits is False, (
        "a 0.9 fraction is reported as fitting: budget {} on a device whose "
        "profile is {} and whose co-tenants hold ~{} (Fraction_Cap "
        "{:.2f})".format(
            _gib(finding.budget_bytes),
            _gib(DEVICE_MEMORY_PROFILE_BYTES['arm64_jp6']),
            _gib(MEASURED_CO_TENANCY_BYTES), JP6_FRACTION_CAP))
    assert re.search(r"co-tenan|co-resident|other consumers",
                     finding.message, re.IGNORECASE), (
        "the verdict states no co-tenancy hazard: {!r}".format(
            finding.message))


def test_case2b_no_message_advises_raising_the_fraction_past_the_cap():
    """A failing verdict at a fraction already above the cap must not offer
    "raise gpu_memory_utilization" as the remedy (defect 1.3); the
    demand-reducing remediations must lead, and the never-lower invariant
    from the sibling spec still holds.

    30 GiB of weights at ``util = 0.9`` fails under the shipped formula too
    (27.00 GiB budget vs 31.00 GiB required), so this exercises the failing
    branch's remediation text directly — the text the operator is handed on
    a device whose co-tenants already hold ~6 GiB."""
    findings = evaluate_fit(
        {'gpu_memory_utilization': 0.9},
        int(30 * GIB),
        ['arm64_jp6'])
    message = findings[0].message

    assert not re.search(r"raise\s+.{0,20}gpu_memory_utilization",
                         message, re.IGNORECASE), (
        "the remediation still advises raising the fraction although 0.9 is "
        "already above the {:.2f} Fraction_Cap: {!r}".format(
            JP6_FRACTION_CAP, message))
    assert re.search(r"max_model_len|smaller model|limit_mm_per_prompt|"
                     r"free .{0,20}memory", message, re.IGNORECASE), (
        "the remediation offers nothing that reduces demand: {!r}".format(
            message))
    # Preserved sibling invariant (S5): never advise LOWERING the fraction.
    assert not re.search(r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization",
                         message, re.IGNORECASE), message


# ---------------------------------------------------------------------------
# Case 3 — the per-architecture publish escape (defect 1.8)
# ---------------------------------------------------------------------------

def _load_publish_module():
    """Load functions/greengrass_publish.py under a distinct module name
    (inside the moto mock, so its module-level boto3 resource and table
    names bind to the test stack)."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish_jp6_kv_explore", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish_jp6_kv_explore"] = module
    spec.loader.exec_module(module)
    return module


class _FakePaginator:
    """Serves list_components / list_component_versions from the fake's own
    registered state (the surface the vLLM version derivation uses)."""

    def __init__(self, fake, operation):
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs):
        if self.operation == "list_components":
            yield {"components": [
                {"componentName": name,
                 "arn": (f"arn:aws:greengrass:{REGION}:123456789012:"
                         f"components:{name}")}
                for name in sorted(self.fake.registered)
            ]}
        elif self.operation == "list_component_versions":
            name = str(kwargs["arn"]).split(":components:")[1].split(":")[0]
            yield {"componentVersions": [
                {"componentVersion": version}
                for version in sorted(self.fake.registered.get(name, ()))
            ]}
        else:  # pragma: no cover - unexpected paginator in the publish path
            raise AssertionError(f"unexpected paginator: {self.operation}")


class FakeGreengrass:
    """moto has no greengrassv2: record what the publish would register."""

    def __init__(self):
        self.created = []
        self.deleted = []
        self.registered = {}

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        self.created.append(recipe)
        self.registered.setdefault(recipe["ComponentName"], set()).add(
            recipe["ComponentVersion"])
        arn = (f"arn:aws:greengrass:{REGION}:123456789012:components:"
               f"{recipe['ComponentName']}:versions:"
               f"{recipe['ComponentVersion']}")
        return {"arn": arn}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.deleted.append(arn)

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)


@pytest.fixture(scope="module")
def pub_env(aws_stack):
    """Training-jobs + models tables + the real greengrass_publish module."""
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    mp.setenv("MODELS_TABLE", MODELS_TABLE_NAME)

    client = boto3.client("dynamodb", region_name=REGION)
    for table in (TRAINING_JOBS_TABLE_NAME, MODELS_TABLE_NAME):
        key = "training_id" if table == TRAINING_JOBS_TABLE_NAME else "model_id"
        client.create_table(
            TableName=table,
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    module = _load_publish_module()
    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=module,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        models=resource.Table(MODELS_TABLE_NAME),
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
        audit_log=aws_stack.tables.audit_log,
    )
    mp.undo()


@pytest.fixture
def seeded(pub_env, monkeypatch):
    """Fresh Use_Case + DataScientist, no polling sleeps, fake Greengrass."""
    monkeypatch.setattr(pub_env.module.time, "sleep", lambda s: None)
    gg = FakeGreengrass()
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    usecase_id = f"uc-{uuid.uuid4()}"
    pub_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "JP6 KV Exploration Use Case",
        "account_id": "123456789012",
        "s3_bucket": "test-vllm-usecase-bucket",
    })
    user_id = f"user-{uuid.uuid4()}"
    pub_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id, gg=gg)


def _seed_vllm_record(pub_env, seeded):
    """A packaged vLLM_Model_Record as packaging.py leaves it, carrying the
    incident's ``gpu_memory_utilization``."""
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": "JP6 KV Escape LLM",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/some-vlm"},
        "engine_configuration": {
            "dtype": "auto",
            "gpu_memory_utilization": str(INCIDENT_UTILIZATION),
            "max_model_len": 4096,
            "tensor_parallel_size": 1,
            "enforce_eager": True,
            # The authored multimodal limit as `resolve_engine_configuration`
            # leaves it since the 2026-08-19 video widening: ONE multimodal
            # unit. Authoring it keeps this case about the per-architecture
            # ESCAPE (defect 1.8) instead of about the unbounded-video term.
            "limit_mm_per_prompt": {"image": 1, "video": 0},
        },
        "packaged_components": [{
            "target": "jetson-xavier-jp6",
            "status": "packaged",
            "component_package_s3": (
                "s3://test-vllm-usecase-bucket/model_artifacts/model-abc/"
                "abc_greengrass_model_component.zip"),
            "supported_architectures": ["arm64_jp6"],
        }],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    pub_env.training_jobs.put_item(Item=item)
    return item


def _publish_event(training_id, user_id, extra_body=None):
    body = {"component_name": "model-caller-chosen",
            "component_version": "9.0.0"}
    body.update(extra_body or {})
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/publish",
        "pathParameters": {"id": training_id},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {
            "sub": user_id,
            "email": f"{user_id}@example.com",
            "cognito:username": user_id,
        }}},
    }


# 20 GiB of weights at util 0.4, ONE authored multimodal unit: required 21 GiB
# under the SHIPPED formula (36 GiB under the corrected one) exceeds the
# 12.00 GiB arm64_jp6 budget but fits the 48.00 GiB arm64_jp7 budget —
# infeasible on the architecture being deployed, feasible on another, which is
# precisely the escape.
JP6_INFEASIBLE_JP7_FEASIBLE_ESTIMATE = WeightEstimate(
    total_bytes=20 * GIB,
    method="safetensors_files",
    detail="synthetic 20 GiB estimate (JP6-infeasible, JP7-feasible)",
)


def test_case3_jp6_infeasible_jp7_feasible_record_must_not_publish(
        pub_env, seeded, monkeypatch):
    """A per-architecture verdict must gate the architecture it applies to.

    Sanity-check of the premise, from the shipped formula itself:
    ``arm64_jp6`` fails (12.00 GiB budget vs 21.00 GiB required) while
    ``arm64_jp7`` passes (48.00 GiB budget) — so ``every_arch_fails`` is
    False and today's gate ships the configuration with at most a warning.
    """
    findings = evaluate_fit(
        {'gpu_memory_utilization': INCIDENT_UTILIZATION,
         'limit_mm_per_prompt': {'image': 1, 'video': 0}},
        JP6_INFEASIBLE_JP7_FEASIBLE_ESTIMATE,
        ['arm64_jp6', 'arm64_jp7'])
    premise = {finding.arch: finding.fits for finding in findings}
    assert premise == {'arm64_jp6': False, 'arm64_jp7': True}, (
        "premise check failed — the estimate is no longer JP6-infeasible / "
        "JP7-feasible under the shipped formula: {}".format(premise))

    record = _seed_vllm_record(pub_env, seeded)
    monkeypatch.setattr(
        pub_env.module, "estimate_weights",
        lambda record, s3_head=None, hf_fetch=None:
            JP6_INFEASIBLE_JP7_FEASIBLE_ESTIMATE)

    response = pub_env.module.publish_component(
        _publish_event(record["training_id"], seeded.user_id), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 422, (
        "a configuration that is infeasible on arm64_jp6 published with "
        "status {} and fit_check {!r}: the per-architecture verdict never "
        "gated the architecture it applies to (registered components: "
        "{})".format(
            response["statusCode"],
            (body.get("fit_check") or {}).get("status"),
            [component["ComponentName"] for component in seeded.gg.created]))
    assert body["fit_check"]["status"] == "failed"
    failing = [finding for finding in body["fit_check"]["findings"]
               if not finding["fits"]]
    assert [finding["arch"] for finding in failing] == ["arm64_jp6"]
    assert seeded.gg.created == [], (
        "components were registered despite the failing architecture: "
        "{}".format([c["ComponentName"] for c in seeded.gg.created]))
