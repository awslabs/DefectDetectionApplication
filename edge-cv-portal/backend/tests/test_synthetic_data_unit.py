"""Unit tests (examples and edge cases) for the synthetic_data Lambda
(synthetic-defect-data-generation, task 4.12).

Covers: empty-catalog guidance message (1.3); last_failure recorded on
Bedrock error (1.4); inpainting vs. image-variation method selection;
randomization defaults per capability flags (4.3); integration response
shape with manifest URI + count (7.6); audit events for create / approve /
integrate (9.4); generation_session_id stored on the training item (8.3)
and failure leaving integration_result intact (8.4); session listing
returning status + creation time (10.4).

Runs against the moto stack from conftest.py + synthetic_env.py.
"""
import json
import uuid

import pytest

from synthetic_env import SyntheticEnv


@pytest.fixture(scope="module")
def senv(aws_stack):
    return SyntheticEnv(aws_stack)


@pytest.fixture
def actor(senv):
    """(usecase_id, DataScientist user) pair, fresh per test."""
    usecase_id = senv.create_usecase()
    return usecase_id, senv.actor_with_role(usecase_id, "DataScientist")


def _patched(senv, name, replacement):
    """Context-manager-free patch helper: returns a restore callable."""
    sd = senv.synthetic_data
    original = getattr(sd, name)
    setattr(sd, name, replacement)
    return lambda: setattr(sd, name, original)


# ------------------------------------------------------ model catalog (1.3)

class TestModelCatalog:
    def test_empty_catalog_returns_guidance_message(self, senv, actor):
        """An empty Model_Catalog intersection returns models: [] plus the
        guidance naming the Bedrock model-access configuration (Req 1.3)."""
        usecase_id, user = actor
        restore = _patched(senv, "_list_available_model_ids", lambda: [])
        try:
            status, body = senv.invoke("GET", "/synthetic/models", user,
                                       query={"usecase_id": usecase_id})
        finally:
            restore()
        assert status == 200
        assert body["models"] == []
        assert "Bedrock" in body["guidance"]
        assert "Model access" in body["guidance"]

    def test_available_models_carry_capability_flags(self, senv, actor):
        usecase_id, user = actor
        restore = _patched(senv, "_list_available_model_ids",
                           lambda: ["amazon.nova-canvas-v1:0"])
        try:
            status, body = senv.invoke("GET", "/synthetic/models", user,
                                       query={"usecase_id": usecase_id})
        finally:
            restore()
        assert status == 200
        assert [m["model_id"] for m in body["models"]] == [
            "amazon.nova-canvas-v1:0"]
        assert body["models"][0]["capabilities"]["inpainting"] is True
        assert "guidance" not in body


# ------------------------------------------- worker failure recording (1.4)

class TestWorkerFailureRecording:
    def test_bedrock_error_records_last_failure_on_session(self, senv,
                                                           actor):
        """A Bedrock invocation error is recorded as last_failure on the
        session META and as failure_reason on the preview (Req 1.4, 4.5)."""
        usecase_id, user = actor
        session_id = senv.put_session_meta(usecase_id, status="generating")
        source_key = f"datasets/unit/{session_id}.png"
        senv.s3.put_object(Bucket=senv.bucket, Key=source_key,
                           Body=b"src")
        senv.sessions_table.update_item(
            Key={"session_id": session_id, "sk": "META"},
            UpdateExpression="SET generation_plan = :p, "
                             "generation_pass = :g",
            ExpressionAttributeValues={
                ":p": [{
                    "task_index": 0, "source_index": 0,
                    "source_image": {"bucket": senv.bucket,
                                     "key": source_key},
                    "variation_index": 0,
                    "model_id": "amazon.nova-canvas-v1:0",
                    "resolved_prompt": "a scratch", "seed": 7,
                    "params": {},
                }],
                ":g": 1,
            })

        def failing_invoke(model_id, request_body):
            raise RuntimeError("ThrottlingException: bedrock says no")

        restore = _patched(senv, "_invoke_image_model", failing_invoke)
        try:
            result = senv.synthetic_data.run_generation_worker({
                "internal_action": "generation_worker",
                "session_id": session_id,
                "generation_pass": 1,
            })
        finally:
            restore()

        assert result["failed"] == 1 and result["completed"] == 0
        meta = senv.sessions_table.get_item(
            Key={"session_id": session_id, "sk": "META"})["Item"]
        assert "bedrock says no" in meta["last_failure"]["reason"]
        previews = [i for i in senv.sessions_table.query(
            KeyConditionExpression="session_id = :s",
            ExpressionAttributeValues={":s": session_id})["Items"]
            if str(i["sk"]).startswith("PREVIEW#")]
        assert len(previews) == 1
        assert previews[0]["status"] == "failed"
        assert "bedrock says no" in previews[0]["failure_reason"]
        # Worker still finishes the pass: status moves to awaiting_review.
        assert meta["status"] == "awaiting_review"


# ------------------------------ method selection and randomization (4.3)

class TestGenerationMethodAndDefaults:
    def test_normal_sources_use_inpainting_when_supported(self, senv):
        sd = senv.synthetic_data
        nova = sd._model_entry("amazon.nova-canvas-v1:0")
        assert sd._select_generation_method("normal", nova) == "inpainting"

    def test_normal_sources_fall_back_to_variation(self, senv):
        sd = senv.synthetic_data
        no_inpaint = {"capabilities": {"inpainting": False,
                                       "image_variation": True}}
        assert sd._select_generation_method("normal", no_inpaint) == \
            "image_variation"

    def test_defect_sources_use_image_variation(self, senv):
        sd = senv.synthetic_data
        nova = sd._model_entry("amazon.nova-canvas-v1:0")
        assert sd._select_generation_method("defect", nova) == \
            "image_variation"

    def test_randomization_defaults_per_capability_flags(self, senv):
        """cfgScale defaults from the model's randomization_defaults and
        seed is included iff the capability flag allows it (Req 4.3)."""
        sd = senv.synthetic_data
        nova = sd._model_entry("amazon.nova-canvas-v1:0")
        body = sd._build_image_request(nova, "image_variation", "p",
                                       "b64", 42, {}, None)
        config = body["imageGenerationConfig"]
        assert config["numberOfImages"] == 1
        assert config["seed"] == 42
        assert config["cfgScale"] == nova["randomization_defaults"][
            "cfg_scale"]

        # Explicit cfg_scale overrides the default.
        body = sd._build_image_request(nova, "image_variation", "p",
                                       "b64", 42, {"cfg_scale": 3.5}, None)
        assert body["imageGenerationConfig"]["cfgScale"] == 3.5

        # A model without seed/cfg capabilities gets neither.
        bare = {"capabilities": {}, "randomization_defaults": {}}
        body = sd._build_image_request(bare, "image_variation", "p",
                                       "b64", 42, {"cfg_scale": 3.5}, None)
        assert "seed" not in body["imageGenerationConfig"]
        assert "cfgScale" not in body["imageGenerationConfig"]

    def test_inpainting_request_shape(self, senv):
        sd = senv.synthetic_data
        nova = sd._model_entry("amazon.nova-canvas-v1:0")
        body = sd._build_image_request(nova, "inpainting", "prompt-text",
                                       "b64img", None, {},
                                       "the scratch region")
        assert body["taskType"] == "INPAINTING"
        assert body["inPaintingParams"]["image"] == "b64img"
        assert body["inPaintingParams"]["maskPrompt"] == \
            "the scratch region"
        assert body["inPaintingParams"]["text"] == "prompt-text"
        assert body["imageGenerationConfig"]["numberOfImages"] == 1


# --------------------------------------------------------- helpers

def _integrated_session(senv, usecase_id, user, approved=2):
    """Create a session with approved staged previews and run a full
    integrate; returns (session_id, integrate_body)."""
    run_id = uuid.uuid4().hex[:12]
    target_prefix = f"datasets/unit-{run_id}/"
    manifest_key = f"{target_prefix}manifests/train.manifest"
    session_id = senv.put_session_meta(
        usecase_id, status="awaiting_review",
        target_dataset_prefix=target_prefix,
        target_manifest_key=manifest_key)
    for index in range(approved):
        preview_id = senv.put_preview(session_id,
                                      approval_state="approved",
                                      variation_index=index)
        senv.s3.put_object(
            Bucket=senv.bucket,
            Key=f"synthetic-staging/{session_id}/{preview_id}.png",
            Body=f"generated-{index}".encode())
    status, body = senv.invoke(
        "POST", "/synthetic/sessions/{id}/integrate", user,
        session_id=session_id)
    assert status == 200, body
    return session_id, body


# ------------------------------------------ integration response (7.6)

class TestIntegrationResponse:
    def test_response_contains_manifest_uri_and_count(self, senv, actor):
        """The integration confirmation includes the updated manifest S3
        URI and the count of appended records (Req 7.6)."""
        usecase_id, user = actor
        session_id, body = _integrated_session(senv, usecase_id, user,
                                               approved=3)
        assert body["appended_count"] == 3
        assert body["manifest_uri"].startswith(f"s3://{senv.bucket}/")
        assert body["manifest_uri"].endswith("train.manifest")
        assert body["status"] == "integrated"

    def test_zero_approved_rejected(self, senv, actor):
        """Confirming with zero approved previews is rejected (Req 6.5)."""
        usecase_id, user = actor
        session_id = senv.put_session_meta(usecase_id)
        senv.put_preview(session_id, approval_state="pending")
        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/integrate", user,
            session_id=session_id)
        assert status == 400
        assert "at least one approved" in body["error"].lower()


# ------------------------------------------------- audit events (9.4)

class TestAuditEvents:
    def test_create_approve_integrate_audited(self, senv, actor):
        """Session create / approve / integrate each log an audit event
        with user, Use_Case, and session id (Req 9.4)."""
        usecase_id, user = actor
        run_id = uuid.uuid4().hex[:12]
        status, created = senv.invoke(
            "POST", "/synthetic/sessions", user,
            body={"usecase_id": usecase_id,
                  "defect_type": "scratch",
                  "target_dataset_prefix": f"datasets/audit-{run_id}/",
                  "target_manifest_key":
                      f"datasets/audit-{run_id}/manifests/train.manifest"})
        assert status == 201
        session_id = created["session"]["session_id"]

        entries = senv.audit_entries("create_generation_session",
                                     user["user_id"])
        assert [e for e in entries
                if e["resource_id"] == session_id
                and e["details"]["usecase_id"] == usecase_id]

        preview_id = senv.put_preview(session_id)
        status, _ = senv.invoke(
            "POST", "/synthetic/sessions/{id}/previews/approval", user,
            session_id=session_id,
            body={"preview_ids": [preview_id],
                  "approval_state": "approved"})
        assert status == 200
        entries = senv.audit_entries("approve_generation_session",
                                     user["user_id"])
        assert [e for e in entries if e["resource_id"] == session_id]

        senv.s3.put_object(
            Bucket=senv.bucket,
            Key=f"synthetic-staging/{session_id}/{preview_id}.png",
            Body=b"generated")
        status, _ = senv.invoke(
            "POST", "/synthetic/sessions/{id}/integrate", user,
            session_id=session_id)
        assert status == 200
        entries = senv.audit_entries("integrate_generation_session",
                                     user["user_id"])
        assert [e for e in entries
                if e["resource_id"] == session_id
                and e["details"]["usecase_id"] == usecase_id]


# ----------------------------------------------- retrain (8.3, 8.4)

class TestRetrain:
    def _retrain_body(self):
        return {
            "model_name": "unit-model",
            "model_version": "1",
            "model_type": "classification",
            "instance_type": "ml.g4dn.2xlarge",
        }

    def test_generation_session_id_stored_on_training_item(self, senv,
                                                           actor):
        """The originating Generation_Session id is stored on the training
        item (Req 8.3)."""
        usecase_id, user = actor
        session_id, _ = _integrated_session(senv, usecase_id, user)
        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/retrain", user,
            session_id=session_id, body=self._retrain_body())
        assert status == 201, body
        item = senv.training_jobs_table.get_item(
            Key={"training_id": body["training_id"]})["Item"]
        assert item["generation_session_id"] == session_id
        assert item["usecase_id"] == usecase_id

    def test_failed_creation_leaves_integration_result_intact(self, senv,
                                                              actor):
        """A failed training creation surfaces the reason while the
        session's integration_result stays intact for retry (Req 8.4)."""
        usecase_id, user = actor
        session_id, integrate_body = _integrated_session(senv, usecase_id,
                                                         user)
        before = senv.sessions_table.get_item(
            Key={"session_id": session_id, "sk": "META"})["Item"][
            "integration_result"]

        bad_body = self._retrain_body()
        bad_body["model_type"] = "not-a-model-type"
        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/retrain", user,
            session_id=session_id, body=bad_body)
        assert status == 400
        assert "error" in body

        after = senv.sessions_table.get_item(
            Key={"session_id": session_id, "sk": "META"})["Item"][
            "integration_result"]
        assert after == before
        assert str(after["manifest_uri"]) == integrate_body["manifest_uri"]

    def test_retrain_requires_integrated_manifest(self, senv, actor):
        usecase_id, user = actor
        session_id = senv.put_session_meta(usecase_id)
        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/retrain", user,
            session_id=session_id, body=self._retrain_body())
        assert status == 400
        assert "manifest" in body["error"].lower()


# ------------------------------------------------ session listing (10.4)

class TestSessionListing:
    def test_listing_returns_status_and_creation_time(self, senv, actor):
        usecase_id, user = actor
        first = senv.put_session_meta(usecase_id, status="draft",
                                      created_at=100)
        second = senv.put_session_meta(usecase_id,
                                       status="awaiting_review",
                                       created_at=200)
        status, body = senv.invoke("GET", "/synthetic/sessions", user,
                                   query={"usecase_id": usecase_id})
        assert status == 200
        by_id = {s["session_id"]: s for s in body["sessions"]}
        assert by_id[first]["status"] == "draft"
        assert by_id[first]["created_at"] == 100
        assert by_id[second]["status"] == "awaiting_review"
        assert by_id[second]["created_at"] == 200
        # Newest first (GSI descending).
        ids = [s["session_id"] for s in body["sessions"]
               if s["session_id"] in (first, second)]
        assert ids == [second, first]
