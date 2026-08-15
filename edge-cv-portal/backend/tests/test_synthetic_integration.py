"""moto-backed integration tests for end-to-end synthetic data wiring
(synthetic-defect-data-generation, task 4.13).

Covers: the full integrate flow landing approved images under the target
dataset prefix (7.3); the appended manifest passing the real
``validate_marketplace_manifest`` logic (7.8); presigned source/preview
reuse following the dataset discovery patterns (3.1, 3.5); and the
retrain path posting to the existing training contract with mocked
SageMaker (8.2). Example-based (1-3 examples each), not property tests.
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
    usecase_id = senv.create_usecase()
    return usecase_id, senv.actor_with_role(usecase_id, "DataScientist")


def _setup_session_with_approved(senv, usecase_id, approved=2,
                                 pre_manifest_records=None):
    """Session (awaiting review) with approved staged previews and,
    optionally, an existing manifest."""
    run_id = uuid.uuid4().hex[:12]
    target_prefix = f"datasets/int-{run_id}/"
    manifest_key = f"{target_prefix}manifests/train.manifest"
    session_id = senv.put_session_meta(
        usecase_id, status="awaiting_review",
        defect_type="scratch",
        target_dataset_prefix=target_prefix,
        target_manifest_key=manifest_key)
    preview_ids = []
    for index in range(approved):
        preview_id = senv.put_preview(session_id,
                                      approval_state="approved",
                                      variation_index=index,
                                      resolved_prompt=f"prompt-{index}")
        senv.s3.put_object(
            Bucket=senv.bucket,
            Key=f"synthetic-staging/{session_id}/{preview_id}.png",
            Body=f"generated-image-{index}".encode())
        preview_ids.append(preview_id)
    if pre_manifest_records is not None:
        content = "".join(json.dumps(r) + "\n"
                          for r in pre_manifest_records)
        senv.s3.put_object(Bucket=senv.bucket, Key=manifest_key,
                           Body=content.encode())
    return session_id, target_prefix, manifest_key, preview_ids


class TestIntegrateFlow:
    def test_approved_images_land_under_target_prefix(self, senv, actor):
        """The full integrate flow copies every approved image under
        {target_dataset_prefix}synthetic/{session_id}/ (Req 7.3) and marks
        non-approved previews rejected (Req 6.6)."""
        usecase_id, user = actor
        session_id, target_prefix, manifest_key, preview_ids = \
            _setup_session_with_approved(senv, usecase_id, approved=2)
        rejected_id = senv.put_preview(session_id,
                                       approval_state="pending")

        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/integrate", user,
            session_id=session_id)
        assert status == 200, body

        listing = senv.s3.list_objects_v2(
            Bucket=senv.bucket,
            Prefix=f"{target_prefix}synthetic/{session_id}/")
        landed = {obj["Key"] for obj in listing.get("Contents", [])}
        expected = {
            f"{target_prefix}synthetic/{session_id}/{pid}.png"
            for pid in preview_ids}
        assert landed == expected

        # Non-approved previews were marked rejected, and no rejected
        # image landed in the dataset.
        item = senv.sessions_table.get_item(Key={
            "session_id": session_id,
            "sk": f"PREVIEW#{rejected_id}"})["Item"]
        assert item["approval_state"] == "rejected"
        assert not any(rejected_id in key for key in landed)

    def test_appended_manifest_passes_real_training_validation(
            self, senv, actor):
        """The updated manifest passes the Training_Subsystem's real
        validate_marketplace_manifest logic unchanged (Req 7.8), for both
        a fresh manifest and one with pre-existing records (Req 7.5)."""
        usecase_id, user = actor
        pre_record = {
            "source-ref": f"s3://{senv.bucket}/datasets/pre/img0.png",
            "anomaly-label": 0,
            "anomaly-label-metadata": {"confidence": 1.0,
                                       "class-name": "normal",
                                       "human-annotated": "yes"},
        }
        senv.s3.put_object(Bucket=senv.bucket, Key="datasets/pre/img0.png",
                           Body=b"pre-image")
        session_id, _prefix, manifest_key, _ids = \
            _setup_session_with_approved(
                senv, usecase_id, approved=2,
                pre_manifest_records=[pre_record])

        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/integrate", user,
            session_id=session_id)
        assert status == 200, body

        # Existing record preserved unchanged and first (Req 7.5).
        content = senv.s3.get_object(
            Bucket=senv.bucket, Key=manifest_key)["Body"].read().decode()
        lines = [json.loads(line) for line in content.splitlines() if line]
        assert lines[0] == pre_record
        assert len(lines) == 3

        # Appended records carry the synthetic metadata (Req 7.4, 10.3).
        for record in lines[1:]:
            assert record["synthetic-defect-metadata"]["synthetic"] is True
            assert record["synthetic-defect-metadata"][
                "generation-session-id"] == session_id
            assert record["anomaly-label-metadata"]["class-name"] == \
                "scratch"

        # The real Training_Subsystem validation accepts the manifest
        # (Req 7.8).
        from shared_utils import get_usecase
        usecase = get_usecase(usecase_id)
        result = senv.training.validate_marketplace_manifest(
            body["manifest_uri"], usecase, "classification")
        assert result["valid"] is True, result

    def test_session_detail_returns_presigned_previews(self, senv, actor):
        """GET /synthetic/sessions/{id} serves completed previews through
        presigned URLs, reusing the dataset preview presigning pattern
        (Req 3.1, 3.5, 5.2)."""
        usecase_id, user = actor
        session_id = senv.put_session_meta(usecase_id)
        preview_id = senv.put_preview(session_id)
        staging_key = f"synthetic-staging/{session_id}/{preview_id}.png"
        senv.s3.put_object(Bucket=senv.bucket, Key=staging_key,
                           Body=b"thumb")
        failed_id = senv.put_preview(session_id, status="failed",
                                     failure_reason="boom",
                                     staging_key=None)

        status, body = senv.invoke("GET", "/synthetic/sessions/{id}",
                                   user, session_id=session_id)
        assert status == 200
        previews = {p["preview_id"]: p for p in body["previews"]}
        url = previews[preview_id]["thumbnail_url"]
        assert url and senv.bucket in url and staging_key in url
        assert "X-Amz-Signature" in url or "Signature" in url
        # Failed previews carry their reason instead of a thumbnail.
        assert previews[failed_id].get("thumbnail_url") is None
        assert previews[failed_id]["failure_reason"] == "boom"


class TestRetrainContract:
    def test_retrain_posts_to_existing_training_contract(self, senv,
                                                         actor):
        """The retrain path creates the training job through the existing
        Training_Subsystem contract (mocked SageMaker via moto), with the
        manifest URI pre-populated from the integration result (Req 8.2)."""
        usecase_id, user = actor
        session_id, _prefix, _key, _ids = _setup_session_with_approved(
            senv, usecase_id, approved=1)
        status, integrate_body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/integrate", user,
            session_id=session_id)
        assert status == 200, integrate_body

        status, body = senv.invoke(
            "POST", "/synthetic/sessions/{id}/retrain", user,
            session_id=session_id,
            body={"model_name": "retrain-model", "model_version": "1",
                  "model_type": "classification"})
        assert status == 201, body
        assert body["training_job_name"].startswith("retrain-model-")
        assert body["status"] == "InProgress"

        # The training item follows the existing contract, with the
        # integrated manifest URI and the originating session (Req 8.2,
        # 8.3).
        item = senv.training_jobs_table.get_item(
            Key={"training_id": body["training_id"]})["Item"]
        assert item["dataset_manifest_s3"] == \
            integrate_body["manifest_uri"]
        assert item["generation_session_id"] == session_id
        assert item["model_type"] == "classification"
        assert item["training_job_arn"]
