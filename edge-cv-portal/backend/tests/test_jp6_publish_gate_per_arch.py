"""
Per-architecture publish gate branch tests for
functions/greengrass_publish.py (jp6-vllm-kv-cache-oom-regression
task 4.2, design Decision 2 / Fix Implementation File 2).

**Validates: Requirements 2.8, 3.1**
# Validates: Requirements 2.8, 3.1

Property 3 (Bug Condition — per-architecture publish gate): for any
findings set where at least one supported architecture fails, the fixed
publish refuses with 422 and the per-architecture findings unless
`skip_fit_check` is supplied, in which case it proceeds with status
'overridden' and records the override in the audit event. The
non-failing statuses keep their meaning: all-fit -> 'passed', a soft
warning on a passing finding -> 'warnings', an undeterminable estimate
-> 'unverified' (never blocking).

Branches covered, each driven by CRAFTED findings so the gate decision
logic is exercised in isolation from the sizing arithmetic (which task
4.1's suites own):

- any-arch fail -> 422: the failing architecture(s) are named in the
  error text (passing ones are not), ALL findings ride in
  `fit_check.findings`, no component registration happens, and the
  record stays in its pre-publish state
- any-arch fail + `skip_fit_check` -> proceed, status 'overridden',
  the override recorded on the audit event
- every architecture fits, no soft warnings -> status 'passed'
- every architecture fits, one finding carries a soft warning
  (thin_margin) -> status 'warnings', publish proceeds
- estimate undeterminable -> status 'unverified', proceeds non-blocking
  and `evaluate_fit` is never consulted

Runs on the existing moto + FakeGreengrass harness from
test_vllm_publish_fit_gate.py; the `estimate_weights` and
`evaluate_fit` module-attribute seams are monkeypatched so no network,
S3 access, or real sizing happens. Honesty guard: this proves the
gate's decision logic, statuses, response shapes and audit records
only — no GPU or device claim is made.

_Requirements: 2.8, 3.1_
"""
import importlib.util
import json
import re
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION
from vllm_fit_check import GIB, FitFinding, WeightEstimate

# Reuse the existing harness (fake Greengrass client, record seeding and
# event/record/audit helpers). The helpers take `pub_env` as an argument,
# so they bind to THIS module's tables, not the sibling's.
from test_vllm_publish_fit_gate import (
    _PUBLISH_PATH,
    FakeGreengrass,
    audit_events,
    publish_event,
    seed_vllm_record,
    stored_record,
)

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-jp6-per-arch-gate"
MODELS_TABLE_NAME = "test-models-jp6-per-arch-gate"


def _load_publish_module():
    """Load functions/greengrass_publish.py under a distinct module name
    (inside the moto mock, so its module-level boto3 resource and table
    names bind to this module's test stack)."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish_per_arch_gate", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish_per_arch_gate"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Environment (same shape as the sibling harness, distinct table names)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pub_env(aws_stack):
    """Training-jobs + models tables + real greengrass_publish module."""
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
    """Fresh Use_Case + DataScientist; no 2s polling sleeps; fake GG."""
    monkeypatch.setattr(pub_env.module.time, "sleep", lambda s: None)
    gg = FakeGreengrass()
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    usecase_id = f"uc-{uuid.uuid4()}"
    pub_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "JP6 Per-Arch Gate Use Case",
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


# ---------------------------------------------------------------------------
# Crafted-findings seams
# ---------------------------------------------------------------------------

ESTIMATE = WeightEstimate(
    total_bytes=6 * GIB,
    method="safetensors_files",
    detail="synthetic 6 GiB estimate (per-arch gate test)",
)


def patch_seams(pub_env, monkeypatch, estimate, spec=None):
    """Monkeypatch BOTH module-attribute seams.

    `spec` maps arch -> {'fits': bool, 'warnings': [...]}; the fake
    evaluate_fit builds one crafted FitFinding per architecture the GATE
    passes in (so the findings always align with the gate's own supported
    set, in its order). With spec=None, evaluate_fit must never be
    consulted (the unverified branch).

    Returns the list of architecture lists evaluate_fit was called with.
    """
    calls = []

    def fake_estimate_weights(record, s3_head=None, hf_fetch=None):
        return estimate

    def fake_evaluate_fit(engine_configuration, est, architectures):
        assert spec is not None, (
            "evaluate_fit was consulted although the estimate was "
            "undeterminable (the unverified branch must skip the fit check)")
        assert est is estimate
        archs = list(architectures)
        calls.append(archs)
        findings = []
        for arch in archs:
            arch_spec = spec[arch]
            fits = arch_spec["fits"]
            findings.append(FitFinding(
                arch=arch,
                fits=fits,
                budget_bytes=9 * GIB,
                required_bytes=(8 if fits else 12) * GIB,
                message=(f"synthetic {'passing' if fits else 'FAILING'} "
                         f"finding for {arch} (test)"),
                weights_bytes=estimate.total_bytes,
                warnings=list(arch_spec.get("warnings", [])),
            ))
        return findings

    monkeypatch.setattr(pub_env.module, "estimate_weights",
                        fake_estimate_weights)
    monkeypatch.setattr(pub_env.module, "evaluate_fit", fake_evaluate_fit)
    return calls


def publish_audit_events(pub_env, user_id):
    return [e for e in audit_events(pub_env, user_id)
            if e["action"] == "publish_greengrass_component"]


# ---------------------------------------------------------------------------
# Branch: ANY architecture fails -> 422 with the per-arch findings (2.8)
# ---------------------------------------------------------------------------

def test_single_arch_failure_blocks_publish_with_422(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    calls = patch_seams(pub_env, monkeypatch, ESTIMATE, spec={
        "arm64_jp6": {"fits": False},
        "arm64_jp7": {"fits": True},
    })

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 422, response["body"]
    body = json.loads(response["body"])

    # The gate consulted the fit check over its full supported set.
    assert calls == [["arm64_jp6", "arm64_jp7"]]

    # 'failed' with ALL findings carried, passing ones included.
    assert body["fit_check"]["status"] == "failed"
    assert {f["arch"]: f["fits"] for f in body["fit_check"]["findings"]} \
        == {"arm64_jp6": False, "arm64_jp7": True}
    assert body["fit_check"]["estimate"]["total_bytes"] == 6 * GIB

    # Only the FAILING architecture is named in the error text.
    assert "arm64_jp6" in body["error"]
    assert "arm64_jp7" not in body["error"]
    # The never-lower invariant holds on the gate's own text too.
    assert not re.search(
        r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization",
        body["error"], re.IGNORECASE), body["error"]

    # No component registration; record in its pre-publish state.
    assert seeded.gg.created == []
    stored = stored_record(pub_env, record["training_id"])
    assert "published" not in stored
    assert "published_component" not in stored
    assert "published_components" not in stored
    assert stored["updated_at"] == record["updated_at"]


def test_multiple_failing_archs_are_all_named_in_the_error(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_seams(pub_env, monkeypatch, ESTIMATE, spec={
        "arm64_jp6": {"fits": False},
        "arm64_jp7": {"fits": False},
    })

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 422, response["body"]
    body = json.loads(response["body"])

    assert body["fit_check"]["status"] == "failed"
    assert {f["arch"]: f["fits"] for f in body["fit_check"]["findings"]} \
        == {"arm64_jp6": False, "arm64_jp7": False}

    # Every failing architecture is named in the error text.
    assert "arm64_jp6" in body["error"]
    assert "arm64_jp7" in body["error"]

    assert seeded.gg.created == []
    assert "published" not in stored_record(pub_env, record["training_id"])


# ---------------------------------------------------------------------------
# Branch: any-arch fail + skip_fit_check -> 'overridden', audited (2.8, 3.1)
# ---------------------------------------------------------------------------

def test_any_arch_failure_with_skip_fit_check_proceeds_overridden(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_seams(pub_env, monkeypatch, ESTIMATE, spec={
        "arm64_jp6": {"fits": False},
        "arm64_jp7": {"fits": True},
    })

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id,
                      extra_body={"skip_fit_check": True}), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    # Status 'overridden', all findings retained, the failing arch and the
    # override named in the annotation.
    assert body["fit_check"]["status"] == "overridden"
    assert {f["arch"]: f["fits"] for f in body["fit_check"]["findings"]} \
        == {"arm64_jp6": False, "arm64_jp7": True}
    assert "arm64_jp6" in body["fit_check"]["message"]
    assert "skip_fit_check" in body["fit_check"]["message"]

    # The publish actually proceeded through component registration.
    assert len(seeded.gg.created) == 1
    assert seeded.gg.created[0]["ComponentName"] == \
        "model-vllm-fit-gate-llm-jetson-xavier-jp6"
    assert stored_record(pub_env, record["training_id"])["published"] is True

    # The override is recorded on the audit event.
    events = publish_audit_events(pub_env, seeded.user_id)
    assert len(events) == 1
    assert events[0]["result"] == "success"
    assert events[0]["details"]["skip_fit_check"] is True


# ---------------------------------------------------------------------------
# Branch: every architecture fits, no soft warnings -> 'passed' (3.1)
# ---------------------------------------------------------------------------

def test_all_architectures_fit_publishes_as_passed(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_seams(pub_env, monkeypatch, ESTIMATE, spec={
        "arm64_jp6": {"fits": True},
        "arm64_jp7": {"fits": True},
    })

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    assert body["fit_check"]["status"] == "passed"
    assert body["fit_check"]["estimate"]["total_bytes"] == 6 * GIB
    findings = body["fit_check"]["findings"]
    assert {f["arch"]: f["fits"] for f in findings} == {
        "arm64_jp6": True, "arm64_jp7": True}
    assert all(f["warnings"] == [] for f in findings)

    # Published, and no override on the audit event.
    assert len(seeded.gg.created) == 1
    assert stored_record(pub_env, record["training_id"])["published"] is True
    events = publish_audit_events(pub_env, seeded.user_id)
    assert len(events) == 1
    assert events[0]["result"] == "success"
    assert "skip_fit_check" not in events[0]["details"]


# ---------------------------------------------------------------------------
# Branch: fits everywhere but a soft warning is present -> 'warnings' (3.1)
# ---------------------------------------------------------------------------

def test_soft_warning_on_a_passing_finding_publishes_as_warnings(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_seams(pub_env, monkeypatch, ESTIMATE, spec={
        "arm64_jp6": {"fits": True, "warnings": ["thin_margin"]},
        "arm64_jp7": {"fits": True},
    })

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    # 'warnings' keeps a meaning under the any-arch gate: fits everywhere,
    # with a recorded caution carried on the finding.
    assert body["fit_check"]["status"] == "warnings"
    by_arch = {f["arch"]: f for f in body["fit_check"]["findings"]}
    assert by_arch["arm64_jp6"]["fits"] is True
    assert by_arch["arm64_jp6"]["warnings"] == ["thin_margin"]
    assert by_arch["arm64_jp7"]["warnings"] == []

    # A soft warning never blocks: the publish proceeded, no override.
    assert len(seeded.gg.created) == 1
    assert stored_record(pub_env, record["training_id"])["published"] is True
    events = publish_audit_events(pub_env, seeded.user_id)
    assert len(events) == 1
    assert events[0]["result"] == "success"
    assert "skip_fit_check" not in events[0]["details"]


# ---------------------------------------------------------------------------
# Branch: undeterminable estimate -> 'unverified', proceeds non-blocking (3.1)
# ---------------------------------------------------------------------------

def test_unverified_estimate_proceeds_without_consulting_the_gate(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    # spec=None arms the fake evaluate_fit to fail the test if consulted.
    patch_seams(pub_env, monkeypatch, estimate=None, spec=None)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    assert body["fit_check"]["status"] == "unverified"
    assert body["fit_check"]["estimate"] is None
    assert body["fit_check"]["findings"] == []

    # Publish went through normally; no override recorded.
    assert len(seeded.gg.created) == 1
    assert stored_record(pub_env, record["training_id"])["published"] is True
    events = publish_audit_events(pub_env, seeded.user_id)
    assert len(events) == 1
    assert "skip_fit_check" not in events[0]["details"]
