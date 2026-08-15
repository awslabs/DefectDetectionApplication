"""
DDA Data Labeling worker (dda-data-labeling, task 7.1).

Async-invoked worker Lambda (fire-and-forget `lambda_client.invoke`
with InvocationType='Event' from dda_labeling.py / labeling.py). The
handler dispatches on the payload's `action` field:

    {action: 'distribute',         job_id}              task 7.1 (this module)
    {action: 'notify_new_members', job_id, member_ids}  task 7.2 (this module)
    {action: 'generate_manifest',  job_id}              task 12.1 (this module)

`distribute` (Requirements 5.1, 5.2, 5.6):

1. Load the job from the labeling jobs table.
2. Re-enumerate the dataset images via get_s3_client_for_bucket using
   the job's dataset_bucket/dataset_prefix — the same enumeration
   (and JPEG/PNG filtering) create_dda_job used, so task order matches
   creation-time image order.
3. Team jobs: apply the shared-layer labeling_distribution.distribute
   round-robin over the enumerated images and the team members
   *currently* holding the Data_Labeler role (roles re-resolved at
   distribution time, Req 5.1); write one Task_Assignment item per
   image (`task-<zero-padded index>`, status=Assigned) with
   batch_writer.
   Skip-verification jobs (Req 9.4): one result item per image with
   assignee_user_id='AUTO' and prelabel_status='Pending', and the
   job's autolabel_pending counter initialized to image_count
   (formalized by task 11.1; the AUTO-item creation shares this code
   path).
4. Verify the written count equals the job's image_count; on any
   shortfall set the job Failed with a failure_reason and mark every
   written task Inactive so no partial set is labelable (Req 5.6).
5. When auto-labeling or skip-verification is enabled, enqueue one SQS
   auto-label message per image on AUTOLABEL_QUEUE_URL (guarded when
   unset): {job_id, task_id, image_s3_uri, modality, label_set, model,
   per_label_prompts?}.
6. Team jobs: run the notification step (`send_distribution_
   notifications`, task 7.4, Req 6.1-6.7): one SES email per member
   holding >= 1 Task_Assignment with the job name, the member's
   assigned image count, and the Labeler_Interface link; per-recipient
   retry with terminal failures recorded on the job item; the whole
   step skipped (notifications_skipped=true) when SES_SENDER_ADDRESS
   is unset.

`generate_manifest` (task 12.1 — Requirements 9.9, 9.11, 10.1-10.6,
10.8, 10.9, 11.6, 11.7, 12.4, 12.5), invoked async by the last labeler
submission (dda_labeling.submit_labeler_task) or by the skip-
verification Admin_Review finalize (task 11.3):

1. Gather the included annotations (Req 10.2): team jobs — every task
   with status='Submitted' (annotation inline, or loaded from
   annotation_s3_key in the portal artifacts bucket); skip-verification
   jobs — exactly the tasks with review_decision='accepted', whose
   annotation is the Bedrock pre-label at prelabel_s3_key (Req 9.9).
   Rejected, failed, and unsubmitted tasks are excluded.
2. Build AnnotationRecord dicts for the shared-layer dda_manifest
   module: source_ref = image_s3_uri, annotation = canonical payload,
   human_annotated from the task for team jobs / False for
   skip-verification (Req 9.11), creation_date = submitted_at_iso or
   the resolution timestamp.
3. Segmentation: render each mask with dda_manifest.render_mask_png
   and the job-wide color map (build_color_map over the Label_Set),
   write masks to s3://{output_bucket}/labeled/{job_id}/masks/
   {image_stem}.png (keys never contain colons) through
   get_s3_client_for_bucket (cross-account role with direct fallback,
   Req 12.4), and set each record's mask_s3_uri.
4. Serialize with dda_manifest.serialize_manifest and run the emitted
   lines through the existing validation path — the shared-layer
   manifest_transformer.detect_ground_truth_attributes plus the
   manifest_validator-style checks for the job's task type. A
   validation failure is a generation failure (Req 10.6).
5. Success: write the manifest to s3://{output_bucket}/labeled/
   {job_id}/output.manifest, record output_manifest_s3_uri (the same
   field GT jobs use, Req 10.8), set status='Completed' + completed_at
   via a conditional update (status = InProgress), and write the
   job_completed audit event — only after the manifest write and
   validation succeed (Req 10.1, 11.6, 11.7).
6. Failure at any point: no manifest URI recorded, annotations
   untouched, status='Failed' with failure_reason (Req 10.9, 12.5).
"""
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    get_s3_client_for_bucket,
    get_usecase,
    log_audit_event,
)
from labeling_distribution import distribute
# Manifest serialization + mask rendering are shared-layer pure
# functions (task 1.3); validation reuses the existing GT-attribute
# detection the training/compile flows consume (Req 10.6).
from dda_manifest import build_color_map, render_mask_png, serialize_manifest
from manifest_transformer import detect_ground_truth_attributes

# Cognito / team-member role resolution and dataset enumeration are
# shared with the API handler so distribution-time behavior matches
# creation-time behavior exactly (Req 5.1: members holding the
# Data_Labeler role *at the time of distribution*; Req 4.5/4.7:
# identical image filtering).
import dda_labeling

# AWS clients
dynamodb = boto3.resource('dynamodb')
sqs_client = boto3.client('sqs')
ses_client = boto3.client('ses')
# Portal-account S3: segmentation annotations (annotation_s3_key) and
# skip-verification pre-labels (prelabel_s3_key) live in the portal
# artifacts bucket. Masks and manifests go to the use case's output
# bucket through get_s3_client_for_bucket instead (Req 12.4).
s3_client = boto3.client('s3')

# Environment configuration
LABELING_JOBS_TABLE = os.environ.get('LABELING_JOBS_TABLE', 'LabelingJobs')
LABELING_TASKS_TABLE = os.environ.get(
    'LABELING_TASKS_TABLE', 'dda-portal-labeling-tasks')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')

labeling_jobs_table = dynamodb.Table(LABELING_JOBS_TABLE)
labeling_tasks_table = dynamodb.Table(LABELING_TASKS_TABLE)

# task-<zero-padded index> sort keys stay lexicographically ordered up
# to a million images per job.
TASK_ID_PAD = 6
# Skip-verification result items are not assigned to a labeler (Req 9.4).
AUTO_ASSIGNEE = 'AUTO'
# Tasks left without a labeler after the last member's removal (Req 5.4).
UNASSIGNED_ASSIGNEE = 'UNASSIGNED'
# SQS send_message_batch accepts at most 10 entries per call.
SQS_BATCH_SIZE = 10
# Req 6.3: one initial SES send attempt plus up to 2 retries.
NOTIFICATION_MAX_ATTEMPTS = 3
# Short backoff between per-recipient retry attempts (seconds).
NOTIFICATION_RETRY_DELAY_SECONDS = 2

# Manifest generation (task 12.1). The existing training/compile
# consumers require these exact DDA attributes per task type (the
# training.py validate-manifest check); detect_ground_truth_attributes
# is fed the lowercase GT task-type identifiers those modules use.
REQUIRED_MANIFEST_ATTRIBUTES = {
    'Classification': (
        'source-ref', 'anomaly-label', 'anomaly-label-metadata'),
    'Segmentation': (
        'source-ref', 'anomaly-label', 'anomaly-label-metadata',
        'anomaly-mask-ref', 'anomaly-mask-ref-metadata'),
    'ObjectDetection': (
        'source-ref', 'bounding-box', 'bounding-box-metadata'),
}
GT_TASK_TYPES = {
    'Classification': 'classification',
    'Segmentation': 'segmentation',
    'ObjectDetection': 'object-detection',
}
# The known GT timestamp bug manifest_validator.py flags: colons in
# mask object keys (Req 10.4 — DDA mask keys never contain colons).
TIMESTAMP_COLON_PATTERN = re.compile(r'T\d{2}:\d{2}:\d{2}\.')


def handler(event, context):
    """Action dispatcher for the async worker.

    Actions:
        distribute         create Task_Assignments for a new DDA job
        generate_manifest  serialize the DDA manifest (task 12.1)
    """
    action = (event or {}).get('action')
    job_id = (event or {}).get('job_id')
    logger.info(f"Worker invoked: action={action} job_id={job_id}")

    if action == 'distribute':
        return distribute_job(job_id)

    if action == 'notify_new_members':
        return notify_new_members(job_id, (event or {}).get('member_ids'))

    if action == 'generate_manifest':
        return generate_manifest_job(job_id)

    message = f"Unknown worker action: {action!r}"
    logger.error(message)
    return {'error': message}


# ---------------------------------------------------------------------------
# distribute (Requirements 5.1, 5.2, 5.6, 9.4)
# ---------------------------------------------------------------------------

def distribute_job(job_id: str) -> Dict[str, Any]:
    """Create the job's Task_Assignments (or skip-verification result
    items), verify completeness, fan out auto-label messages, and run
    the notification hook."""
    if not job_id:
        message = 'distribute requires a job_id'
        logger.error(message)
        return {'error': message}

    job = labeling_jobs_table.get_item(Key={'job_id': job_id}).get('Item')
    if not job:
        message = f"Labeling job {job_id} not found"
        logger.error(message)
        return {'error': message, 'job_id': job_id}

    if job.get('status') != 'InProgress':
        # A stop/failure raced the async invoke; never distribute work
        # for a job that is no longer in progress.
        logger.warning(f"Job {job_id} is {job.get('status')}; "
                       f"skipping distribution")
        return {'job_id': job_id, 'action': 'distribute', 'skipped': True,
                'status': job.get('status')}

    try:
        return _distribute(job)
    except Exception as e:  # noqa: BLE001 — Req 5.6: never leave a
        # partially distributed job labelable.
        logger.error(f"Distribution failed for job {job_id}: {e}",
                     exc_info=True)
        _fail_distribution(job, f"Task distribution failed: {e}")
        return {'job_id': job_id, 'action': 'distribute',
                'status': 'Failed', 'error': str(e)}


def _distribute(job: Dict) -> Dict[str, Any]:
    job_id = job['job_id']
    image_count = int(job['image_count'])
    skip_verification = bool(job.get('skip_verification'))
    auto_label = job.get('auto_label') or {}
    autolabel_enabled = skip_verification or bool(auto_label.get('enabled'))
    # Tasks awaiting a pre-label are withheld from labelers until it is
    # Available or Failed (Req 8.6/8.7); without auto-labeling there is
    # nothing to wait for.
    prelabel_status = 'Pending' if autolabel_enabled else 'None'

    # Re-enumerate the dataset with the exact creation-time filtering
    # (JPEG/PNG, nested prefixes, folder markers skipped) so task-to-
    # image mapping matches the validated enumeration (Req 5.1).
    usecase = get_usecase(job['usecase_id'])
    s3_client = get_s3_client_for_bucket(
        usecase, job['dataset_bucket'], 'dda-labeling-distribute')
    images, _ = dda_labeling._enumerate_dataset_images(
        s3_client, job['dataset_bucket'], job['dataset_prefix'])

    task_ids = [f"task-{index:0{TASK_ID_PAD}d}"
                for index in range(len(images))]

    if skip_verification:
        # Req 9.4: no labeler Task_Assignments — one AUTO result item
        # per image, reviewed by the admin when auto-labeling finishes.
        assignments = {task_id: AUTO_ASSIGNEE for task_id in task_ids}
    else:
        members = dda_labeling._team_data_labeler_members(
            job['team_id'], job['usecase_id'])
        member_ids = [member['user_id'] for member in members]
        # Req 5.1/5.2: exactly one task per image, each to exactly one
        # current Data_Labeler, per-member counts differing by <= 1.
        # With zero eligible members this is empty and the written-count
        # verification below fails the job (Req 5.6).
        assignments = distribute(task_ids, member_ids)

    now = int(datetime.utcnow().timestamp())
    try:
        with labeling_tasks_table.batch_writer() as batch:
            for task_id, image_key in zip(task_ids, images):
                assignee = assignments.get(task_id)
                if not assignee:
                    continue
                batch.put_item(Item={
                    'job_id': job_id,
                    'task_id': task_id,
                    'image_s3_uri':
                        f"s3://{job['dataset_bucket']}/{image_key}",
                    'image_key': image_key,
                    'usecase_id': job['usecase_id'],
                    'assignee_user_id': assignee,
                    'status': 'Assigned',
                    'prelabel_status': prelabel_status,
                    'created_at': now,
                })
    except Exception as e:  # noqa: BLE001 — verified (and failed) below
        logger.error(f"batch_writer failed for job {job_id}: {e}",
                     exc_info=True)

    # Req 5.6: verify one written task per enumerated image; on any
    # shortfall fail the job and deactivate whatever was written.
    written_count = _count_job_tasks(job_id)
    if written_count != image_count:
        reason = (f"Task distribution wrote {written_count} of "
                  f"{image_count} task assignments")
        logger.error(f"Job {job_id}: {reason}")
        _fail_distribution(job, reason)
        return {'job_id': job_id, 'action': 'distribute',
                'status': 'Failed', 'error': reason}

    if skip_verification:
        # Task 11.1 formalizes the counter; it is initialized here
        # because the AUTO items share this code path. The auto-label
        # worker decrements it per resolved image and flips
        # review_ready at zero (Req 9.5).
        labeling_jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET autolabel_pending = :pending, '
                             'updated_at = :now',
            ExpressionAttributeValues={':pending': image_count,
                                       ':now': now},
        )

    if autolabel_enabled:
        _enqueue_autolabel_messages(job, task_ids, images)

    if not skip_verification:
        # Req 9.4: skip-verification jobs send zero labeler
        # notifications; team jobs notify after distribution (Req 6.1).
        send_distribution_notifications(job, assignments)

    logger.info(f"Distributed {written_count} tasks for job {job_id} "
                f"(skip_verification={skip_verification}, "
                f"autolabel={autolabel_enabled})")
    return {'job_id': job_id, 'action': 'distribute',
            'status': 'InProgress', 'task_count': written_count}


def _count_job_tasks(job_id: str) -> int:
    """Number of task items stored for the job (written-count
    verification, Req 5.6)."""
    count = 0
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'job_id = :jid',
        'ExpressionAttributeValues': {':jid': job_id},
        'Select': 'COUNT',
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        count += response.get('Count', 0)
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return count


def _job_task_ids(job_id: str) -> List[str]:
    """Every stored task_id for the job."""
    task_ids: List[str] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'job_id = :jid',
        'ExpressionAttributeValues': {':jid': job_id},
        'ProjectionExpression': 'task_id',
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        task_ids.extend(item['task_id']
                        for item in response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return task_ids


def _fail_distribution(job: Dict, reason: str) -> None:
    """Req 5.6: set the job Failed with the failure reason and mark
    every written task Inactive so no partial set is labelable."""
    job_id = job['job_id']
    for task_id in _job_task_ids(job_id):
        try:
            labeling_tasks_table.update_item(
                Key={'job_id': job_id, 'task_id': task_id},
                UpdateExpression='SET #status = :inactive',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={':inactive': 'Inactive'},
            )
        except Exception as e:  # noqa: BLE001 — keep deactivating the rest
            logger.error(f"Could not deactivate task {task_id} of job "
                         f"{job_id}: {e}")
    try:
        labeling_jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET #status = :failed, '
                             'failure_reason = :reason, '
                             'updated_at = :now',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':failed': 'Failed',
                ':reason': reason,
                ':now': int(datetime.utcnow().timestamp()),
            },
        )
    except Exception as e:  # noqa: BLE001 — nothing further to unwind
        logger.error(f"Could not mark job {job_id} Failed: {e}")


# ---------------------------------------------------------------------------
# Auto-label SQS fan-out (Requirements 8.2, 9.4 — consumed by
# dda_autolabel_worker.py, task 10.1)
# ---------------------------------------------------------------------------

def _enqueue_autolabel_messages(job: Dict, task_ids: List[str],
                                images: List[str]) -> None:
    """One SQS message per image on the auto-label queue. Guarded when
    AUTOLABEL_QUEUE_URL is unset: distribution proceeds without the
    fan-out (pre-labels simply never arrive)."""
    queue_url = os.environ.get('AUTOLABEL_QUEUE_URL')
    if not queue_url:
        logger.warning(f"AUTOLABEL_QUEUE_URL is not set; skipping "
                       f"auto-label fan-out for job {job['job_id']}")
        return

    skip_verification = bool(job.get('skip_verification'))
    if skip_verification:
        model = f"bedrock:{job['bedrock_model_id']}"
    else:
        model = (job.get('auto_label') or {}).get('model')

    entries = []
    for task_id, image_key in zip(task_ids, images):
        message: Dict[str, Any] = {
            'job_id': job['job_id'],
            'task_id': task_id,
            'image_s3_uri': f"s3://{job['dataset_bucket']}/{image_key}",
            'modality': job['task_type'],
            'label_set': list(job.get('label_set') or []),
            'model': model,
        }
        if skip_verification:
            message['per_label_prompts'] = dict(
                job.get('per_label_prompts') or {})
        entries.append({'Id': str(len(entries)),
                        'MessageBody': json.dumps(message)})

    for start in range(0, len(entries), SQS_BATCH_SIZE):
        batch = entries[start:start + SQS_BATCH_SIZE]
        response = sqs_client.send_message_batch(
            QueueUrl=queue_url, Entries=batch)
        failed = response.get('Failed') or []
        if failed:
            # The auto-label worker withholds Pending tasks until a
            # pre-label resolves; a lost message surfaces there. Log
            # loudly rather than failing the whole distribution.
            logger.error(f"{len(failed)} auto-label messages failed to "
                         f"enqueue for job {job['job_id']}: {failed}")

    logger.info(f"Enqueued {len(entries)} auto-label messages for job "
                f"{job['job_id']}")


# ---------------------------------------------------------------------------
# notify_new_members (task 7.2 — Requirement 6.7)
# ---------------------------------------------------------------------------

def notify_new_members(job_id: str, member_ids: List[str]) -> Dict[str, Any]:
    """Notify members who previously held zero Task_Assignments in the
    job and were just assigned work by a membership-change rebalance
    (Req 6.7). Invoked async by dda_labeling.py's add-member path with
    {action: 'notify_new_members', job_id, member_ids}.

    Builds the {task_id -> assignee} map restricted to exactly the given
    members and dispatches it through send_distribution_notifications —
    the same path initial-distribution notifications use, so these
    members get the standard email (job name, their assigned count,
    labeler link).
    """
    if not job_id or not member_ids:
        message = 'notify_new_members requires a job_id and member_ids'
        logger.error(message)
        return {'error': message, 'job_id': job_id}

    job = labeling_jobs_table.get_item(Key={'job_id': job_id}).get('Item')
    if not job:
        message = f"Labeling job {job_id} not found"
        logger.error(message)
        return {'error': message, 'job_id': job_id}

    member_set = set(member_ids)
    assignments: Dict[str, str] = {}
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'job_id = :jid',
        'ProjectionExpression': 'task_id, assignee_user_id',
        'ExpressionAttributeValues': {':jid': job_id},
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        for item in response.get('Items', []):
            if item.get('assignee_user_id') in member_set:
                assignments[item['task_id']] = item['assignee_user_id']
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key

    send_distribution_notifications(job, assignments)
    notified = sorted({assignee for assignee in assignments.values()})
    logger.info(f"notify_new_members for job {job_id}: "
                f"{len(notified)} member(s), {len(assignments)} task(s)")
    return {'job_id': job_id, 'action': 'notify_new_members',
            'member_ids': notified, 'task_count': len(assignments)}


# ---------------------------------------------------------------------------
# Notification service (task 7.4 — Requirements 6.1-6.7)
# ---------------------------------------------------------------------------

def send_distribution_notifications(job: Dict,
                                    assignments: Dict[str, str]) -> None:
    """SES notification service (Req 6.1-6.7).

    Called after a successful team-job distribution (and by the
    notify_new_members action, whose caller already restricted the map
    to members who previously held zero tasks, Req 6.7) with the job
    item and the {task_id -> assignee_user_id} assignment map.

    Sends exactly one email per member holding >= 1 Task_Assignment in
    the map (Req 6.1) containing the job name, the recipient's assigned
    image count, and the Labeler_Interface link (Req 6.2), from the
    configured SES sender (Req 6.5). Per-recipient retry with terminal
    failures recorded on the job item while the remaining recipients
    are still processed and the job status is never changed (Req 6.3,
    6.4). When SES_SENDER_ADDRESS is unset the job proceeds with
    notifications_skipped=true and nothing is sent (Req 6.6).

    Never raises: a notification problem must not fail the distribution
    that invoked it (Req 6.4).
    """
    try:
        _send_distribution_notifications(job, assignments)
    except Exception as e:  # noqa: BLE001 — Req 6.4: notification
        # failures never propagate into the distribution outcome.
        logger.error(f"Notification service failed for job "
                     f"{job.get('job_id')}: {e}", exc_info=True)


def _send_distribution_notifications(job: Dict,
                                     assignments: Dict[str, str]) -> None:
    job_id = job['job_id']
    # Req 6.1: recipients are exactly the members holding >= 1 task in
    # the map; AUTO result items and UNASSIGNED placeholders are not
    # members. Members with zero tasks never appear in the map.
    counts = Counter(assignee for assignee in assignments.values()
                     if assignee not in (AUTO_ASSIGNEE,
                                         UNASSIGNED_ASSIGNEE))
    if not counts:
        logger.info(f"No notification recipients for job {job_id}")
        return

    sender = os.environ.get('SES_SENDER_ADDRESS')
    if not sender:
        # Req 6.6: no sender configured — the job proceeds, the skipped
        # state is recorded for the job detail view, nothing is sent.
        logger.warning(f"SES_SENDER_ADDRESS is not set; skipping "
                       f"notifications for job {job_id}")
        _record_notifications_skipped(job_id)
        return

    emails = _resolve_recipient_emails(job, set(counts))
    sent = 0
    for user_id in sorted(counts):
        email = emails.get(user_id)
        if not email:
            _record_notification_failure(
                job_id, user_id,
                'No email address could be resolved for the member')
            continue
        error = _send_notification_email(sender, email, job,
                                          counts[user_id])
        if error:
            # Req 6.4: record and continue with the remaining
            # recipients; the job status is never touched.
            _record_notification_failure(job_id, email, error)
        else:
            sent += 1
    logger.info(f"Notifications for job {job_id}: {sent} of "
                f"{len(counts)} recipient(s) emailed")


def _resolve_recipient_emails(job: Dict,
                              user_ids: Set[str]) -> Dict[str, str]:
    """Resolve member user_ids to email addresses.

    Team member items already carry the member's portal account email
    (persisted at add time), so the job's team is the primary source;
    any member not resolved there falls back to a Cognito lookup via
    USER_POOL_ID (the same source user administration uses).
    """
    emails: Dict[str, str] = {}
    team_id = job.get('team_id')
    if team_id:
        try:
            for member in dda_labeling._team_members(team_id):
                user_id = member.get('user_id')
                if user_id in user_ids and member.get('email'):
                    emails[user_id] = member['email']
        except Exception as e:  # noqa: BLE001 — fall back to Cognito
            logger.warning(f"Could not read team {team_id} members for "
                           f"notification emails: {e}")
    for user_id in user_ids - set(emails):
        try:
            cognito_user = dda_labeling._resolve_cognito_user(user_id)
        except Exception as e:  # noqa: BLE001 — recorded as a failure
            logger.warning(f"Could not resolve Cognito user {user_id}: {e}")
            cognito_user = None
        if cognito_user and cognito_user.get('email'):
            emails[user_id] = cognito_user['email']
    return emails


def _send_notification_email(sender: str, recipient: str, job: Dict,
                             assigned_count: int) -> Optional[str]:
    """Send one notification email with per-recipient retry (Req 6.3:
    up to NOTIFICATION_MAX_ATTEMPTS total attempts with a short
    backoff). Returns None on success, or the terminal failure reason
    once every attempt has failed."""
    job_name = job.get('job_name') or job['job_id']
    portal_domain = os.environ.get('PORTAL_DOMAIN', '')
    # Req 6.2: resolves to the Labeler_Interface sign-in and, after
    # authentication, presents this job to the recipient.
    link = f"https://{portal_domain}/labeler?job={job['job_id']}"
    subject = f"Labeling tasks assigned: {job_name}"
    text_body = (
        f"You have been assigned {assigned_count} image(s) in the "
        f"labeling job '{job_name}'.\n\n"
        f"Start labeling: {link}\n")
    html_body = (
        f"<p>You have been assigned {assigned_count} image(s) in the "
        f"labeling job '<b>{job_name}</b>'.</p>"
        f'<p><a href="{link}">Start labeling</a></p>')

    last_error = 'unknown error'
    for attempt in range(1, NOTIFICATION_MAX_ATTEMPTS + 1):
        try:
            ses_client.send_email(
                Source=sender,
                Destination={'ToAddresses': [recipient]},
                Message={
                    'Subject': {'Data': subject},
                    'Body': {'Text': {'Data': text_body},
                             'Html': {'Data': html_body}},
                })
            return None
        except Exception as e:  # noqa: BLE001 — retried, then recorded
            last_error = str(e)
            logger.warning(f"SES send attempt {attempt}/"
                           f"{NOTIFICATION_MAX_ATTEMPTS} to {recipient} "
                           f"failed for job {job['job_id']}: {e}")
            if attempt < NOTIFICATION_MAX_ATTEMPTS:
                time.sleep(NOTIFICATION_RETRY_DELAY_SECONDS)
    return last_error


def _record_notification_failure(job_id: str, email: str,
                                 reason: str) -> None:
    """Req 6.4: append {email, reason} to the job item's
    notification_failures list; the job status is never changed."""
    logger.error(f"Notification to {email} failed terminally for job "
                 f"{job_id}: {reason}")
    try:
        labeling_jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET notification_failures = list_append('
                             'if_not_exists(notification_failures, '
                             ':empty), :failure), updated_at = :now',
            ExpressionAttributeValues={
                ':empty': [],
                ':failure': [{'email': email, 'reason': reason}],
                ':now': int(datetime.utcnow().timestamp()),
            },
        )
    except Exception as e:  # noqa: BLE001 — keep processing recipients
        logger.error(f"Could not record notification failure for job "
                     f"{job_id}: {e}")


def _record_notifications_skipped(job_id: str) -> None:
    """Req 6.6: record on the job that notifications were skipped so
    the job detail view can surface the state."""
    try:
        labeling_jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET notifications_skipped = :skipped, '
                             'updated_at = :now',
            ExpressionAttributeValues={
                ':skipped': True,
                ':now': int(datetime.utcnow().timestamp()),
            },
        )
    except Exception as e:  # noqa: BLE001 — the job must still proceed
        logger.error(f"Could not record notifications_skipped for job "
                     f"{job_id}: {e}")


# ---------------------------------------------------------------------------
# generate_manifest (task 12.1 — Requirements 9.9, 9.11, 10.1-10.6,
# 10.8, 10.9, 11.6, 11.7, 12.4, 12.5)
# ---------------------------------------------------------------------------

class ManifestGenerationError(Exception):
    """Manifest generation failed: the job is marked Failed with the
    failure reason, no manifest URI is recorded, and every persisted
    annotation is left untouched (Req 10.9, 12.5)."""


def generate_manifest_job(job_id: str) -> Dict[str, Any]:
    """Serialize the job's included annotations into the DDA_Manifest,
    validate it, write it to the use case's output bucket, and complete
    the job (Req 10.1). Any failure marks the job Failed with a
    failure_reason and records no manifest URI (Req 10.9)."""
    if not job_id:
        message = 'generate_manifest requires a job_id'
        logger.error(message)
        return {'error': message}

    job = labeling_jobs_table.get_item(Key={'job_id': job_id}).get('Item')
    if not job:
        message = f"Labeling job {job_id} not found"
        logger.error(message)
        return {'error': message, 'job_id': job_id}

    if job.get('status') != 'InProgress':
        # A stop/failure (or an earlier completion) raced the async
        # invoke; never regenerate or complete a job that is no longer
        # in progress.
        logger.warning(f"Job {job_id} is {job.get('status')}; "
                       f"skipping manifest generation")
        return {'job_id': job_id, 'action': 'generate_manifest',
                'skipped': True, 'status': job.get('status')}

    try:
        return _generate_manifest(job)
    except Exception as e:  # noqa: BLE001 — Req 10.9/12.5: any failure
        # records no manifest URI and leaves annotations untouched.
        logger.error(f"Manifest generation failed for job {job_id}: {e}",
                     exc_info=True)
        _fail_manifest_generation(job, f"Manifest generation failed: {e}")
        return {'job_id': job_id, 'action': 'generate_manifest',
                'status': 'Failed', 'error': str(e)}


def _generate_manifest(job: Dict) -> Dict[str, Any]:
    job_id = job['job_id']
    modality = job.get('task_type')
    if modality not in REQUIRED_MANIFEST_ATTRIBUTES:
        raise ManifestGenerationError(
            f"unsupported task type {modality!r}")
    label_set = [str(name) for name in (job.get('label_set') or [])]
    skip_verification = bool(job.get('skip_verification'))

    # --- included annotations (Req 10.2, 9.9) ---
    tasks = _query_all_job_tasks(job_id)
    if skip_verification:
        # Exactly the accepted Admin_Review results; rejected and
        # failed images are excluded (Req 9.9).
        included = [task for task in tasks
                    if task.get('review_decision') == 'accepted']
    else:
        # Every submitted Task_Assignment; unsubmitted and
        # presentation-failed tasks are excluded (Req 10.2).
        included = [task for task in tasks
                    if task.get('status') == 'Submitted']
    if not included:
        raise ManifestGenerationError(
            'no annotations are eligible for the manifest '
            f"({'zero accepted results' if skip_verification else 'zero submitted tasks'})")

    # --- output bucket via the cross-account mechanism with direct
    # fallback (Req 12.4) ---
    usecase = get_usecase(job['usecase_id'])
    output_bucket = usecase.get('s3_bucket')
    if not output_bucket:
        raise ManifestGenerationError(
            f"use case {job['usecase_id']} has no s3_bucket output "
            f"bucket configured")
    output_s3 = get_s3_client_for_bucket(
        usecase, output_bucket, 'dda-labeling-manifest')

    # Job-wide color map: identical for every image in the job
    # (Req 10.4).
    color_map = (build_color_map(label_set)
                 if modality == 'Segmentation' else None)
    mask_prefix = f"labeled/{job_id}/masks"

    # --- AnnotationRecord assembly (+ mask rendering, masks first,
    # manifest last) ---
    records: List[Dict] = []
    used_stems: Set[str] = set()
    for task in included:
        annotation = _canonical_annotation(
            _load_task_annotation(task, skip_verification), modality)
        record: Dict[str, Any] = {
            'source_ref': task.get('image_s3_uri'),
            'annotation': annotation,
            # Req 9.11: skip-verification results are machine-annotated;
            # team submissions carry the task's human_annotated marker
            # (True, Req 7.7/8.4).
            'human_annotated': (False if skip_verification
                                else bool(task.get('human_annotated',
                                                   True))),
            'creation_date': _annotation_creation_date(task),
        }
        if modality == 'Segmentation':
            image_size = annotation.get('image_size') or {}
            width = int(image_size.get('width') or 0)
            height = int(image_size.get('height') or 0)
            if width < 1 or height < 1:
                raise ManifestGenerationError(
                    f"task {task.get('task_id')} annotation has no "
                    f"valid image_size for mask rendering")
            png_bytes = render_mask_png(
                annotation.get('regions') or [], width, height, color_map)
            mask_key = _mask_object_key(
                mask_prefix, task, used_stems)
            output_s3.put_object(
                Bucket=output_bucket,
                Key=mask_key,
                Body=png_bytes,
                ContentType='image/png',
            )
            record['mask_s3_uri'] = f"s3://{output_bucket}/{mask_key}"
        records.append(record)

    # --- serialization (Req 10.3-10.5) ---
    job_context: Dict[str, Any] = {
        'job_name': job.get('job_name') or job_id,
        'modality': modality,
        'label_set': label_set,
    }
    if color_map is not None:
        job_context['color_map'] = color_map
    lines = serialize_manifest(records, job_context)

    # --- validation gate (Req 10.6): a validation failure is a
    # generation failure ---
    issues = _validate_manifest_lines(lines, modality)
    if issues:
        raise ManifestGenerationError(
            'generated manifest failed validation: ' + '; '.join(issues))

    # --- manifest write, then completion (Req 10.1, 10.8, 11.6) ---
    manifest_key = f"labeled/{job_id}/output.manifest"
    output_s3.put_object(
        Bucket=output_bucket,
        Key=manifest_key,
        Body=('\n'.join(lines) + '\n').encode('utf-8'),
        ContentType='application/json',
    )
    manifest_uri = f"s3://{output_bucket}/{manifest_key}"

    now = int(datetime.utcnow().timestamp())
    set_parts = [
        '#status = :completed',
        'completed_at = :now',
        'updated_at = :now',
        # Req 10.8: the same field GT jobs use, so training/compile
        # consume DDA and GT jobs identically.
        'output_manifest_s3_uri = :uri',
    ]
    values: Dict[str, Any] = {
        ':completed': 'Completed',
        ':inprogress': 'InProgress',
        ':now': now,
        ':uri': manifest_uri,
    }
    if modality == 'Segmentation':
        set_parts.append('mask_output_prefix = :mask_prefix')
        set_parts.append('color_map = :color_map')
        values[':mask_prefix'] = f"s3://{output_bucket}/{mask_prefix}/"
        values[':color_map'] = color_map
    try:
        labeling_jobs_table.update_item(
            Key={'job_id': job_id},
            UpdateExpression='SET ' + ', '.join(set_parts),
            ConditionExpression='#status = :inprogress',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            # A stop raced the generation between the InProgress check
            # and here: leave the raced status (and its lifecycle
            # semantics) intact — no URI recorded, no completion.
            logger.warning(f"Job {job_id} left InProgress state during "
                           f"manifest generation; completion skipped")
            return {'job_id': job_id, 'action': 'generate_manifest',
                    'skipped': True}
        raise

    # Req 11.7: completion audit event. DDA manifest generation is a
    # system step, attributed to the job's creator.
    log_audit_event(
        user_id=job.get('created_by') or 'system',
        action='job_completed',
        resource_type='labeling_job',
        resource_id=job_id,
        result='success',
        details={
            'usecase_id': job.get('usecase_id'),
            'job_name': job.get('job_name'),
            'labeling_backend': 'DDA',
            'output_manifest_s3_uri': manifest_uri,
            'entry_count': len(lines),
        },
    )

    logger.info(f"Manifest for job {job_id} written to {manifest_uri} "
                f"({len(lines)} entries)")
    return {'job_id': job_id, 'action': 'generate_manifest',
            'status': 'Completed',
            'output_manifest_s3_uri': manifest_uri,
            'entry_count': len(lines)}


def _query_all_job_tasks(job_id: str) -> List[Dict]:
    """Every task item of the job, in task_id order."""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'job_id = :jid',
        'ExpressionAttributeValues': {':jid': job_id},
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def _load_task_annotation(task: Dict, skip_verification: bool) -> Dict:
    """The task's persisted annotation payload.

    Team jobs: the inline `annotation` attribute, or the segmentation
    RLE JSON at annotation_s3_key in the portal artifacts bucket.
    Skip-verification jobs: the accepted result's annotation is the
    Bedrock pre-label at prelabel_s3_key (Req 9.9)."""
    task_id = task.get('task_id')
    if skip_verification:
        key = task.get('prelabel_s3_key')
        if not key:
            raise ManifestGenerationError(
                f"accepted task {task_id} has no prelabel_s3_key")
        return _read_artifact_json(key, task_id)

    annotation = task.get('annotation')
    if annotation is not None:
        return annotation
    key = task.get('annotation_s3_key')
    if not key:
        raise ManifestGenerationError(
            f"submitted task {task_id} has no annotation")
    return _read_artifact_json(key, task_id)


def _read_artifact_json(key: str, task_id: Optional[str]) -> Dict:
    if not PORTAL_ARTIFACTS_BUCKET:
        raise ManifestGenerationError(
            'PORTAL_ARTIFACTS_BUCKET is not configured; the stored '
            'annotation cannot be read')
    try:
        response = s3_client.get_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key)
        return json.loads(response['Body'].read())
    except Exception as e:  # noqa: BLE001 — a missing annotation fails
        # the whole generation (Req 10.9), never a partial manifest.
        raise ManifestGenerationError(
            f"annotation for task {task_id} could not be read from "
            f"s3://{PORTAL_ARTIFACTS_BUCKET}/{key}: {e}")


def _plain(value: Any) -> Any:
    """DynamoDB Decimals back to JSON-serializable ints/floats."""
    if isinstance(value, Decimal):
        return (int(value) if value == value.to_integral_value()
                else float(value))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _canonical_annotation(annotation: Any, modality: str) -> Dict:
    """Normalize a persisted annotation into the canonical
    modality-tagged model dda_manifest serializes: DynamoDB Decimals
    become plain numbers, and pre-label payloads carrying
    image_width/image_height (the auto-label worker's shape) gain the
    canonical image_size."""
    if not isinstance(annotation, dict):
        raise ManifestGenerationError(
            f"persisted annotation is not an object: {annotation!r}")
    annotation = _plain(annotation)
    if (modality == 'ObjectDetection'
            and 'image_size' not in annotation
            and annotation.get('image_width') is not None
            and annotation.get('image_height') is not None):
        annotation['image_size'] = {
            'width': int(annotation['image_width']),
            'height': int(annotation['image_height']),
        }
    return annotation


def _annotation_creation_date(task: Dict) -> str:
    """The annotation's persisted timestamp (Req 10.3): the labeler
    submission's ISO timestamp, or — for skip-verification results,
    which have no submission — the ISO form of the item's resolution
    timestamp."""
    iso = task.get('submitted_at_iso')
    if iso:
        return str(iso)
    epoch = task.get('submitted_at') or task.get('updated_at')
    if epoch:
        return datetime.utcfromtimestamp(int(epoch)).isoformat() + 'Z'
    return datetime.utcnow().isoformat() + 'Z'


def _mask_object_key(mask_prefix: str, task: Dict,
                     used_stems: Set[str]) -> str:
    """s3 key for the task's rendered mask:
    labeled/{job_id}/masks/{image_stem}.png. Keys never contain colons
    (Req 10.4); duplicate stems from nested prefixes are disambiguated
    with the task id so no mask silently overwrites another."""
    image_key = task.get('image_key') or ''
    if not image_key:
        uri = task.get('image_s3_uri') or ''
        image_key = uri[len('s3://'):].split('/', 1)[1] if '/' in uri else uri
    stem = image_key.rsplit('/', 1)[-1]
    if '.' in stem:
        stem = stem.rsplit('.', 1)[0]
    stem = stem.replace(':', '-') or task.get('task_id', 'mask')
    if stem in used_stems:
        stem = f"{stem}-{task.get('task_id')}"
    used_stems.add(stem)
    return f"{mask_prefix}/{stem}.png"


def _validate_manifest_lines(lines: List[str],
                             modality: str) -> List[str]:
    """The existing validation path over the emitted JSON Lines
    (Req 10.6): the manifest_validator.py checks (per-line JSON
    parsing, non-empty manifest, mask-key timestamp colons) plus the
    training.py required-attribute checks for the task type, and the
    shared-layer detect_ground_truth_attributes gate — the manifest
    must already be in DDA form, needing no transformation. Returns a
    list of issues; empty means valid."""
    issues: List[str] = []
    entries: List[Dict] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            issues.append(f"line {index + 1}: invalid JSON - {e}")
    if issues:
        return issues
    if not entries:
        return ['no valid entries found in manifest']

    required = REQUIRED_MANIFEST_ATTRIBUTES[modality]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(f"entry {index + 1}: not a JSON object")
            continue
        missing = [attr for attr in required if attr not in entry]
        if missing:
            issues.append(f"entry {index + 1}: missing required "
                          f"attributes {', '.join(missing)}")
            continue
        if not isinstance(entry.get('source-ref'), str):
            issues.append(f"entry {index + 1}: source-ref must be a "
                          f"string (S3 URI)")
        if modality in ('Classification', 'Segmentation'):
            if not isinstance(entry.get('anomaly-label'), (int, float)) \
                    or isinstance(entry.get('anomaly-label'), bool):
                issues.append(f"entry {index + 1}: anomaly-label must "
                              f"be a number (0 or 1)")
            if not isinstance(entry.get('anomaly-label-metadata'), dict):
                issues.append(f"entry {index + 1}: "
                              f"anomaly-label-metadata must be an object")
        if modality == 'Segmentation':
            mask_ref = entry.get('anomaly-mask-ref')
            if not isinstance(mask_ref, str) \
                    or not mask_ref.startswith('s3://'):
                issues.append(f"entry {index + 1}: anomaly-mask-ref "
                              f"must be an S3 URI")
            else:
                remainder = mask_ref[len('s3://'):]
                mask_key = (remainder.split('/', 1)[1]
                            if '/' in remainder else '')
                if ':' in mask_key or TIMESTAMP_COLON_PATTERN.search(
                        mask_ref):
                    issues.append(
                        f"entry {index + 1}: mask key contains colons "
                        f"(the Ground Truth timestamp bug): {mask_key}")
            if not isinstance(
                    entry.get('anomaly-mask-ref-metadata'), dict):
                issues.append(f"entry {index + 1}: "
                              f"anomaly-mask-ref-metadata must be an "
                              f"object")
        if modality == 'ObjectDetection':
            if not isinstance(entry.get('bounding-box'), dict):
                issues.append(f"entry {index + 1}: bounding-box must "
                              f"be an object")
            if not isinstance(entry.get('bounding-box-metadata'), dict):
                issues.append(f"entry {index + 1}: "
                              f"bounding-box-metadata must be an object")
    if issues:
        return issues

    # detect_ground_truth_attributes gate on the first entry — exactly
    # how manifest_validator.py / training.py invoke it, with the
    # lowercase GT task-type identifier.
    detected = detect_ground_truth_attributes(
        entries[0], GT_TASK_TYPES[modality])
    if modality in ('Classification', 'Segmentation'):
        # The manifest must already carry the canonical DDA attributes:
        # any detected foreign attribute pair means a Ground Truth
        # manifest needing transformation (manifest_validator's
        # needs_transformation condition), which the Manifest_Generator
        # must never emit (Req 10.6).
        if detected and (detected.get('segmentation_only')
                         or detected.get('label_attr') != 'anomaly-label'):
            issues.append(
                'manifest is in Ground Truth format and requires '
                'transformation (detected attribute '
                f"{detected.get('metadata_attr')!r})")
    else:
        # Object detection: the GT bounding-box attribute pair itself
        # is the consumable format; it must be detectable.
        if not detected or detected.get('label_attr') != 'bounding-box':
            issues.append(
                'bounding-box attributes were not detected in the '
                'manifest')
    return issues


def _fail_manifest_generation(job: Dict, reason: str) -> None:
    """Req 10.9/12.5: mark the job Failed with the failure reason —
    no manifest URI is recorded and persisted annotations are left
    untouched. Conditional on InProgress so a concurrent stop's
    terminal status is never overwritten."""
    try:
        labeling_jobs_table.update_item(
            Key={'job_id': job['job_id']},
            UpdateExpression='SET #status = :failed, '
                             'failure_reason = :reason, '
                             'updated_at = :now',
            ConditionExpression='#status = :inprogress',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':failed': 'Failed',
                ':inprogress': 'InProgress',
                ':reason': reason[:1024],
                ':now': int(datetime.utcnow().timestamp()),
            },
        )
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            logger.warning(f"Job {job['job_id']} is no longer "
                           f"InProgress; Failed status not applied")
            return
        logger.error(f"Could not mark job {job['job_id']} Failed: {e}")
    except Exception as e:  # noqa: BLE001 — nothing further to unwind
        logger.error(f"Could not mark job {job['job_id']} Failed: {e}")
