"""
DDA Data Labeling backend (dda-data-labeling).

This handler serves the portal-native labeling APIs registered in the
DdaLabelingApiStack. Task 4.1 implements the Labeling_Team management
routes (Requirements 3.1-3.8); task 5.3 adds `create_dda_job` (invoked
by labeling.py's backend switch, Requirements 4.1-4.11, 8.1, 8.8,
9.1-9.3, 11.3, 11.7, 12.1-12.3); later tasks add the labeler APIs and
the skip-verification admin review on this same handler. The
grounded-sam-autolabel spec adds the `grounded-sam` auto-label model
family to job creation: a Segmentation/ObjectDetection matrix entry and
optional per-label text Prompt_Overrides validated and persisted under
`auto_label.prompt_overrides` (that spec's Requirements 1.5-1.7,
2.4-2.6, 2.8).

Team management routes (permission: labeling-teams:manage —
UseCaseAdmin / PortalAdmin, enforced per request via @rbac_check):

    GET    /labeling-teams?usecase_id=                       list teams
    POST   /labeling-teams                                   create team
    DELETE /labeling-teams/{teamId}                          delete team
    POST   /labeling-teams/{teamId}/members                  add member
    DELETE /labeling-teams/{teamId}/members/{userId}         remove member

Labeler read routes (task 8.1, permission: labeling:tasks-self — the
only APIs a Data_Labeler-only user may call; the Data_Labeler role
carries the permission globally via the Cognito custom:role claim, and
the real authority is the server-side ownership check: the task's
assignee_user_id must equal the caller's sub AND the caller must be a
current member of the job's team, Req 2.4/2.6):

    GET /labeler/jobs                        jobs holding >=1 unsubmitted
                                             task for the caller, with
                                             submitted/remaining/withheld
                                             counts (Req 2.4, 7.10)
    GET /labeler/jobs/{jobId}/next           next presentable unsubmitted
                                             task with a 15-minute
                                             presigned image URL,
                                             pre-label, instructions and
                                             example URLs (Req 7.1, 7.2,
                                             7.11, 8.3, 8.6, 8.7, 12.6)
    GET /labeler/tasks/{taskId}/image-url    fresh 15-minute presigned
                                             URL after expiry (Req 12.7)

Labeler write routes (task 8.4, same permission + ownership checks):

    POST /labeler/tasks/{taskId}/submit      persist a complete
                                             annotation and mark the
                                             task Submitted (Req 7.7,
                                             7.8, 7.9, 8.4, 11.6, 11.8)
    POST /labeler/tasks/{taskId}/presentation-failure
                                             record a presentation
                                             failure; withholds the
                                             task (Req 7.12)

Skip-verification Admin_Review routes (task 11.3, permission:
manage_labeling_jobs via @rbac_check plus an explicit
UseCaseAdmin/PortalAdmin role check matching skip-verification job
creation, Req 9.1; the job's Use_Case scope is injected like the team
routes — labeling.py's /labeling/{id}/stop pattern):

    GET  /labeling/{id}/review               paginated auto-label
                                             results covering every
                                             dataset image with its
                                             succeeded/failed status,
                                             pre-label, decision, and
                                             presigned image URL
                                             (Req 9.5, 9.10)
    POST /labeling/{id}/review/decisions     batch accept/reject
                                             upserts, mutable until
                                             finalized (Req 9.6, 9.10)
    POST /labeling/{id}/review/finalize      all-decided + >=1 accepted
                                             gating, then manifest
                                             generation (Req 9.7, 9.8,
                                             9.9, 11.6)

Storage: single-table `dda-portal-labeling-teams` — one partition per
team (PK team_id) with item-type-prefixed sort keys:
    sk = 'META'              team metadata (usecase_id, team_name, ...)
    sk = 'MEMBER#<user_id>'  member entries (user_id, email, ...)
GSI `usecase-teams-index` (usecase_id, created_at) holds META items only
and backs use-case-scoped listing and per-use-case name uniqueness.
"""
import base64
import json
import os
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response,
    get_s3_client_for_bucket,
    get_usecase,
    get_user_from_event,
    handle_error,
    log_audit_event,
    rbac_manager,
    Permission,
)
from rbac_middleware import rbac_check
from labeling_distribution import rebalance
# LLM auto-labeling model identifier validation shared with the
# per-image consumer (llm-auto-labeling Requirement 1.5).
from dda_llm_guidance import validate_model_identifier
# Few_Shot_Example designations, shared with the request builder both
# the Preview_API and the Auto_Labeler call
# (llm-autolabel-prompt-tuning Requirement 6.4). The selection and
# limit-resolution functions are the same ones the Auto_Labeler calls,
# so a Preview_Run reports exactly the example subset labeling time will
# attach (Requirements 7.1, 7.2, 7.6).
from dda_llm_request import (
    FEW_SHOT_BAD,
    FEW_SHOT_GOOD,
    MODEL_TOKEN_LIMIT_CEILING,
    image_format_for_key,
    resolve_model_image_limit,
    resolve_token_budget,
    select_few_shot_examples,
)
# The one copy of the PNG IHDR / JPEG SOF header parser, relocated
# verbatim to the shared layer (llm-model-token-and-image-sizing
# Req 7.6); `_preview_image_dimensions` below is a thin delegation to
# it. `dda_llm_image` imports no Pillow at import time, so attaching it
# here costs nothing. MAX_IMAGE_EDGE_OPTIONS is the closed
# Downscale_Setting option set the Preview_API validates against
# (Req 5.5) — the same tuple the Image_Downscaler resolves through, so
# a value this route accepts is a value the downscaler applies.
# `normalize_downscale_setting` makes the RUN item's recorded
# Downscale_Setting total and safe for the executor, the same way the
# Auto_Labeler reads the job record's: an absent, null or malformed
# recorded value resolves to Downscale_Off with no failure (Req 5.9,
# 5.12).
from dda_llm_image import (
    MAX_IMAGE_EDGE_OPTIONS,
    declared_dimensions,
    normalize_downscale_setting,
)
# The one implementation of the `llm:` family model invocation, shared
# with the Auto_Labeler (llm-autolabel-prompt-tuning Req 3.1, 3.2): the
# Preview_Run executor calls exactly the function
# `dda_autolabel_worker._generate_llm_prelabel` calls, with the same
# argument construction, so a preview predicts labeling-time behavior
# rather than imitating it. Same functions bundle, so it is imported
# directly, and `bedrock_common.get_bedrock_client` is rebound onto it
# per call for the same reason the worker rebinds its own binding: the
# Bedrock client seam stays this module's, so a stubbed client in tests
# reaches the shared invocation.
import dda_llm_prelabel
from bedrock_common import get_bedrock_client
from dda_llm_prelabel import (
    LlmPrelabelError,
    LlmPrelabelResult,
    generate_llm_prelabel,
)

# AWS clients
dynamodb = boto3.resource('dynamodb')
cognito_client = boto3.client('cognito-idp')
lambda_client = boto3.client('lambda')
# Portal-account S3 (artifacts bucket: pre-labels, example images).
# Dataset images go through get_s3_client_for_bucket instead (cross-
# account role with single-account direct fallback, Req 12.1-12.3).
s3_client = boto3.client('s3')

# Environment configuration
LABELING_TEAMS_TABLE = os.environ.get(
    'LABELING_TEAMS_TABLE', 'dda-portal-labeling-teams')
LABELING_JOBS_TABLE = os.environ.get('LABELING_JOBS_TABLE', 'LabelingJobs')
LABELING_TASKS_TABLE = os.environ.get(
    'LABELING_TASKS_TABLE', 'dda-portal-labeling-tasks')
USER_ROLES_TABLE = os.environ.get('USER_ROLES_TABLE')
USER_POOL_ID = os.environ.get('USER_POOL_ID')
# Pre-labels and example images live in the portal's own artifacts
# bucket (written by dda_autolabel_worker / the job creation wizard).
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
# Portal settings table: holds the persisted Model_Token_Limits item
# (`llm_model_token_limits`) the per-invocation _llm_model_token_limits()
# loader reads (llm-model-token-and-image-sizing Req 1.6, 1.8). Read
# lazily per invocation, never at import, so an environment without the
# settings stack simply falls back to the LLM_MODEL_TOKEN_LIMITS
# deploy-time bootstrap.
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

teams_table = dynamodb.Table(LABELING_TEAMS_TABLE)
labeling_jobs_table = dynamodb.Table(LABELING_JOBS_TABLE)
labeling_tasks_table = dynamodb.Table(LABELING_TASKS_TABLE)

# Sort-key layout of the single-table teams store.
TEAM_META_SK = 'META'
MEMBER_SK_PREFIX = 'MEMBER#'

# Labeling_Team name constraints (Requirement 3.2).
TEAM_NAME_MAX_LENGTH = 128

# DDA job creation constraints (Requirements 4.1-4.4, 8.1, 8.8, 9.2).
JOB_NAME_MAX_LENGTH = 63
LABEL_SET_MAX_CLASSES = 10
LABEL_NAME_MAX_LENGTH = 64
INSTRUCTIONS_MAX_LENGTH = 5000
EXAMPLE_IMAGES_MAX = 10
SUPPORTED_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
CLASSIFICATION_LABEL_SET = ['normal', 'anomaly']
VALID_MODALITIES = ('Classification', 'Segmentation', 'ObjectDetection')
# Auto_Labeler model/modality compatibility matrix (Requirement 8.8):
# SAM is geometry-only (masks/boxes); Bedrock vision models answer
# classification and bounding-box prompts but do not paint masks.
# Prompt-guided LLM auto-labeling covers all three modalities
# (llm-auto-labeling Requirement 1.3). Grounded-SAM produces geometry
# already classified from text prompts, so it covers the two geometry
# modalities but — like sam — never Classification (grounded-sam-
# autolabel Requirements 1.5, 1.6).
AUTO_LABEL_MODEL_MODALITIES = {
    'sam': ('Segmentation', 'ObjectDetection'),
    'grounded-sam': ('Segmentation', 'ObjectDetection'),
    'bedrock': ('Classification', 'ObjectDetection'),
    'llm': ('Classification', 'Segmentation', 'ObjectDetection'),
}
# Detection_Prompt bounds for the llm: family (llm-auto-labeling
# Requirement 2: 1-2000 characters, length judged on the raw string).
DETECTION_PROMPT_MAX_LENGTH = 2000
# Prompt_Override bound for the grounded-sam family (grounded-sam-
# autolabel Requirement 2.6: length judged on the raw string).
PROMPT_OVERRIDE_MAX_LENGTH = 256
# Skip_Verification_Mode is admin-only (Requirement 9.1).
SKIP_VERIFICATION_ADMIN_ROLES = ('UseCaseAdmin', 'PortalAdmin')
# Sentinel assignee for unsubmitted tasks left behind when a team's
# last Data_Labeler is removed (Requirement 5.4).
UNASSIGNED_ASSIGNEE = 'UNASSIGNED'
# Labeler image access grants are read-only presigned GET URLs scoped
# to exactly one object and valid for at most 15 minutes (Req 12.6).
IMAGE_URL_EXPIRY_SECONDS = 900
# Tasks whose Pre_Label generation is still pending are withheld from
# presentation (Req 8.7); None/Available/Failed are presentable.
PENDING_PRELABEL_STATUS = 'Pending'
# Admin_Review listing page size bounds (GET /labeling/{id}/review).
REVIEW_PAGE_SIZE_DEFAULT = 50
REVIEW_PAGE_SIZE_MAX = 100
# The only valid per-image review decisions (Req 9.6).
REVIEW_DECISIONS = ('accepted', 'rejected')


def handler(event, context):
    """Main Lambda handler for DDA labeling operations."""
    # The Model_Token_Limits memo is per invocation, never per container,
    # so a warm container can never serve a mapping written by an earlier
    # invocation (llm-model-token-and-image-sizing Req 1.6, 4.1). Cleared
    # ahead of the action dispatch so the async Preview_Run executor
    # invocation gets its own fresh read too.
    _reset_model_token_limits_cache()
    try:
        # Non-HTTP entry, deliberately ahead of the HTTP dispatch: the
        # Preview_Run executor is *this same function*, self-invoked
        # asynchronously by POST /labeling-preview/runs with
        # {'action': 'execute_preview_run', 'run_id': ...} (task 8.2,
        # llm-autolabel-prompt-tuning Req 8.1). An action payload carries
        # no httpMethod and no resource, so it must be recognized before
        # any routing on those fields — mirroring dda_labeling_worker's
        # action dispatcher.
        action = (event or {}).get('action')
        if action:
            return _handle_preview_action(action, event, context)

        http_method = event.get('httpMethod')
        resource = event.get('resource', '')
        path = event.get('path', '')

        logger.info(f"Handler invoked: {http_method} {path} "
                    f"(resource: {resource})")

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers':
                        'Content-Type,Authorization,X-Amz-Date,X-Api-Key,'
                        'X-Amz-Security-Token',
                    'Access-Control-Allow-Methods':
                        'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        path_params = event.get('pathParameters') or {}
        team_id = path_params.get('teamId')
        member_user_id = path_params.get('userId')

        # Team-scoped routes carry no usecase_id of their own: resolve it
        # from the team record and inject it into the event so @rbac_check
        # authorizes against the team's Use_Case scope (Req 3.7). When the
        # team does not exist the scope falls back to 'global'
        # (allow_global) and the handler answers 404 after authorization.
        if team_id:
            _inject_team_usecase_scope(event, team_id)

        if '/labeling-teams' in resource:
            is_members_route = '/members' in resource
            if http_method == 'GET' and not team_id:
                return list_labeling_teams(event, context)
            elif http_method == 'POST' and not team_id:
                return create_labeling_team(event, context)
            elif http_method == 'DELETE' and team_id and not is_members_route:
                return delete_labeling_team(event, context)
            elif http_method == 'POST' and team_id and is_members_route:
                return add_team_member(event, context)
            elif (http_method == 'DELETE' and team_id and is_members_route
                    and member_user_id):
                return remove_team_member(event, context)

        # Labeler routes (task 8.1 reads; task 8.4 submission and
        # presentation-failure).
        if resource.startswith('/labeler'):
            if http_method == 'GET' and resource == '/labeler/jobs':
                return list_labeler_jobs(event, context)
            if (http_method == 'GET'
                    and resource == '/labeler/jobs/{jobId}/next'):
                return get_next_labeler_task(event, context)
            if (http_method == 'GET'
                    and resource == '/labeler/tasks/{taskId}/image-url'):
                return get_task_image_url(event, context)
            if (http_method == 'POST'
                    and resource == '/labeler/tasks/{taskId}/submit'):
                return submit_labeler_task(event, context)
            if (http_method == 'POST'
                    and resource
                    == '/labeler/tasks/{taskId}/presentation-failure'):
                return report_presentation_failure(event, context)

        # Prompt_Tuning_Preview routes (task 8.2 start; task 8.3 status).
        # The Use_Case scope for @rbac_check comes from the request body
        # (POST) or the run record (GET), injected inside each route so the
        # authorization check runs before every other validation
        # (llm-autolabel-prompt-tuning Req 8.6).
        if resource.startswith('/labeling-preview/runs'):
            if (http_method == 'POST'
                    and resource == '/labeling-preview/runs'):
                return start_preview_run(event, context)
            if (http_method == 'GET'
                    and resource == '/labeling-preview/runs/{runId}'):
                return get_preview_run(event, context)

        # Skip-verification Admin_Review routes (task 11.3). Like the
        # team routes, the job's usecase_id is injected so @rbac_check
        # authorizes MANAGE_LABELING_JOBS in the job's Use_Case scope
        # (labeling.py's /labeling/{id}/stop pattern).
        review_job_id = path_params.get('id')
        if review_job_id and resource.startswith('/labeling/'):
            _inject_job_usecase_scope(event, review_job_id)
            if (http_method == 'GET'
                    and resource == '/labeling/{id}/review'):
                return get_admin_review(event, context)
            if (http_method == 'POST'
                    and resource == '/labeling/{id}/review/decisions'):
                return save_review_decisions(event, context)
            if (http_method == 'POST'
                    and resource == '/labeling/{id}/review/finalize'):
                return finalize_admin_review(event, context)

        return create_response(404, {'error': 'Not found'})

    except Exception as e:
        return handle_error(e, 'DDA labeling operation failed')


# ---------------------------------------------------------------------------
# Team store helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def _get_team_meta(team_id: str) -> Optional[Dict]:
    """The team's META item, or None when the team does not exist."""
    response = teams_table.get_item(
        Key={'team_id': team_id, 'sk': TEAM_META_SK})
    return response.get('Item')


def _inject_team_usecase_scope(event: Dict, team_id: str) -> None:
    """Make the team's usecase_id visible to @rbac_check via the query
    string, so team-scoped routes are authorized in the team's Use_Case
    scope rather than 'global'."""
    meta = _get_team_meta(team_id)
    if meta:
        params = event.get('queryStringParameters') or {}
        params.setdefault('usecase_id', meta['usecase_id'])
        event['queryStringParameters'] = params


def _inject_job_usecase_scope(event: Dict, job_id: str) -> None:
    """Make the job's usecase_id visible to @rbac_check via the query
    string, so job-scoped routes (the /labeling/{id}/review* set) are
    authorized in the job's Use_Case scope rather than 'global'
    (labeling.py's /labeling/{id}/stop pattern). When the job does not
    exist the scope falls back to 'global' (allow_global) and the
    handler answers 404 after authorization."""
    try:
        response = labeling_jobs_table.get_item(Key={'job_id': job_id})
    except Exception as e:  # noqa: BLE001 — scope falls back to global
        logger.warning(f"Could not resolve usecase scope for job "
                       f"{job_id}: {e}")
        return
    job = response.get('Item')
    if job and job.get('usecase_id'):
        params = event.get('queryStringParameters') or {}
        params.setdefault('usecase_id', job['usecase_id'])
        event['queryStringParameters'] = params


def _query_team_items(team_id: str) -> List[Dict]:
    """Every item in the team's partition (META + members)."""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'team_id = :tid',
        'ExpressionAttributeValues': {':tid': team_id},
    }
    while True:
        response = teams_table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def _team_members(team_id: str) -> List[Dict]:
    """The team's MEMBER# items."""
    return [item for item in _query_team_items(team_id)
            if str(item.get('sk', '')).startswith(MEMBER_SK_PREFIX)]


def _query_usecase_team_metas(usecase_id: str) -> List[Dict]:
    """All team META items for a Use_Case (usecase-teams-index holds META
    items only — member items carry no usecase_id/created_at)."""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'IndexName': 'usecase-teams-index',
        'KeyConditionExpression': 'usecase_id = :uc',
        'ExpressionAttributeValues': {':uc': usecase_id},
    }
    while True:
        response = teams_table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def _in_progress_jobs_referencing_team(usecase_id: str,
                                       team_id: str) -> List[Dict]:
    """InProgress labeling jobs in the Use_Case that reference the team."""
    jobs: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'IndexName': 'usecase-jobs-index',
        'KeyConditionExpression': 'usecase_id = :uc',
        'FilterExpression': '#status = :in_progress AND team_id = :tid',
        'ExpressionAttributeNames': {'#status': 'status'},
        'ExpressionAttributeValues': {
            ':uc': usecase_id,
            ':in_progress': 'InProgress',
            ':tid': team_id,
        },
    }
    while True:
        response = labeling_jobs_table.query(**kwargs)
        jobs.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return jobs


# ---------------------------------------------------------------------------
# Membership-change reassignment (task 7.2 — Requirements 3.6, 5.3, 5.4,
# 5.5, 5.7, 6.7)
# ---------------------------------------------------------------------------

def _query_job_assigned_tasks(job_id: str, assignee_user_id: str) -> List[Dict]:
    """The job's unsubmitted (status=Assigned) tasks held by one
    assignee. Submitted tasks and their annotations are never returned,
    so reassignment can never touch them (Req 5.3)."""
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'job_id = :jid',
        'FilterExpression': '#status = :assigned '
                            'AND assignee_user_id = :assignee',
        'ExpressionAttributeNames': {'#status': 'status'},
        'ExpressionAttributeValues': {
            ':jid': job_id,
            ':assigned': 'Assigned',
            ':assignee': assignee_user_id,
        },
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def _job_assignee_counts(job_id: str) -> Dict[str, int]:
    """Task count per assignee across the whole job (any status) —
    identifies members who previously held zero Task_Assignments in the
    job for the Req 6.7 notification."""
    counts: Dict[str, int] = {}
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'job_id = :jid',
        'ExpressionAttributeValues': {':jid': job_id},
        'ProjectionExpression': 'assignee_user_id',
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        for item in response.get('Items', []):
            assignee = item.get('assignee_user_id')
            if assignee:
                counts[assignee] = counts.get(assignee, 0) + 1
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return counts


def _conditional_reassign(job_id: str, task_id: str, from_assignee: str,
                          to_assignee: str) -> bool:
    """Move one unsubmitted task to a new assignee with a conditional
    update (`status = Assigned AND assignee_user_id = from_assignee`).

    Returns False when the condition fails (a concurrent submission or
    reassignment won the race), True on success. Any other error is
    raised to the caller, which rolls back and reports the failure
    (Req 5.7).
    """
    try:
        labeling_tasks_table.update_item(
            Key={'job_id': job_id, 'task_id': task_id},
            UpdateExpression='SET assignee_user_id = :to_assignee, '
                             'updated_at = :now',
            ConditionExpression='#status = :assigned '
                                'AND assignee_user_id = :from_assignee',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':to_assignee': to_assignee,
                ':from_assignee': from_assignee,
                ':assigned': 'Assigned',
                ':now': int(datetime.utcnow().timestamp()),
            },
        )
        return True
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            return False
        raise


def _rollback_reassignments(applied: List[tuple]) -> None:
    """Req 5.7: restore the prior assignments from the computed inverse
    of the reassignments already applied. Each rollback write is itself
    conditional on the task still holding the reassigned state, so a
    submission that landed mid-rollback is never overwritten."""
    for job_id, task_id, prior_assignee, new_assignee in reversed(applied):
        try:
            restored = _conditional_reassign(
                job_id, task_id, new_assignee, prior_assignee)
            if not restored:
                logger.error(
                    f"Rollback could not restore task {task_id} of job "
                    f"{job_id} to {prior_assignee} (concurrent change)")
        except Exception as e:  # noqa: BLE001 — keep restoring the rest
            logger.error(f"Rollback failed for task {task_id} of job "
                         f"{job_id}: {e}")


def _set_job_blocked(job_id: str, blocked: bool) -> None:
    """Req 5.4/5.5: flip the job's blocked indication; status is never
    changed here (stays InProgress)."""
    labeling_jobs_table.update_item(
        Key={'job_id': job_id},
        UpdateExpression='SET blocked = :blocked, updated_at = :now',
        ExpressionAttributeValues={
            ':blocked': blocked,
            ':now': int(datetime.utcnow().timestamp()),
        },
    )


def _reassign_tasks_for_member_removal(meta: Dict, team_id: str,
                                       removed_user_id: str) -> Optional[Dict]:
    """Reassign the removed member's unsubmitted tasks in every
    InProgress DDA job assigned to the team (Req 5.3), or park them as
    UNASSIGNED and block the job when the last member is removed
    (Req 5.4). Returns None on success or an error API response on
    failure, in which case all prior assignments have been restored and
    the membership must not be deleted (Req 5.7).
    """
    usecase_id = meta['usecase_id']
    jobs = _in_progress_jobs_referencing_team(usecase_id, team_id)
    if not jobs:
        return None

    # Remaining members currently holding the Data_Labeler role
    # (re-resolved now, like distribution time — Req 5.3).
    remaining_ids = sorted(
        member['user_id']
        for member in _team_data_labeler_members(team_id, usecase_id)
        if member['user_id'] != removed_user_id)

    applied: List[tuple] = []   # (job_id, task_id, prior, new)
    blocked_job_ids: List[str] = []
    try:
        for job in jobs:
            job_id = job['job_id']
            tasks = _query_job_assigned_tasks(job_id, removed_user_id)
            if not tasks:
                continue
            task_ids = sorted(task['task_id'] for task in tasks)

            if remaining_ids:
                # Req 5.3: balanced (<= 1 spread) round-robin over the
                # remaining members, unsubmitted tasks only.
                assignments = rebalance(task_ids, remaining_ids)
            else:
                # Req 5.4: last member removed — tasks go UNASSIGNED and
                # the job is marked blocked below.
                assignments = {task_id: UNASSIGNED_ASSIGNEE
                               for task_id in task_ids}

            for task_id in task_ids:
                new_assignee = assignments[task_id]
                if not _conditional_reassign(
                        job_id, task_id, removed_user_id, new_assignee):
                    raise _ReassignmentConflict(
                        f"Task {task_id} of job {job_id} changed "
                        f"concurrently during reassignment")
                applied.append(
                    (job_id, task_id, removed_user_id, new_assignee))

            if not remaining_ids:
                _set_job_blocked(job_id, True)
                blocked_job_ids.append(job_id)
    except Exception as e:  # noqa: BLE001 — Req 5.7: full rollback
        logger.error(f"Member-removal reassignment failed for team "
                     f"{team_id}: {e}", exc_info=True)
        _rollback_reassignments(applied)
        for job_id in blocked_job_ids:
            try:
                _set_job_blocked(job_id, False)
            except Exception as unblock_error:  # noqa: BLE001
                logger.error(f"Could not clear blocked flag on job "
                             f"{job_id} during rollback: {unblock_error}")
        status_code = 409 if isinstance(e, _ReassignmentConflict) else 500
        return create_response(status_code, {
            'error': 'Reassignment of the member\'s unsubmitted tasks '
                     'could not be completed; the membership and all '
                     'prior task assignments are unchanged',
            'reason': str(e),
            'team_id': team_id,
            'user_id': removed_user_id,
        })
    return None


def _rebalance_blocked_jobs_for_member_addition(
        meta: Dict, team_id: str, added_user_id: str) -> Optional[Dict]:
    """Req 5.5: for each blocked InProgress job of the team, distribute
    the UNASSIGNED tasks across the team's current Data_Labeler members
    (balanced <= 1 spread) and clear the blocked indication. Members who
    previously held zero tasks in a job are notified through the worker
    (Req 6.7). Returns None on success or an error API response after a
    full rollback (Req 5.7).
    """
    usecase_id = meta['usecase_id']
    blocked_jobs = [job for job
                    in _in_progress_jobs_referencing_team(usecase_id, team_id)
                    if job.get('blocked')]
    if not blocked_jobs:
        return None

    member_ids = sorted(
        member['user_id']
        for member in _team_data_labeler_members(team_id, usecase_id))
    if added_user_id not in member_ids:
        # The just-persisted member always participates even if the
        # eventually-consistent member query missed it.
        member_ids = sorted(member_ids + [added_user_id])

    applied: List[tuple] = []          # (job_id, task_id, prior, new)
    unblocked_job_ids: List[str] = []
    notifications: List[Dict] = []     # deferred until all jobs succeed
    try:
        for job in blocked_jobs:
            job_id = job['job_id']
            prior_counts = _job_assignee_counts(job_id)
            unassigned = _query_job_assigned_tasks(
                job_id, UNASSIGNED_ASSIGNEE)
            task_ids = sorted(task['task_id'] for task in unassigned)
            assignments = rebalance(task_ids, member_ids)

            for task_id in task_ids:
                new_assignee = assignments[task_id]
                if not _conditional_reassign(
                        job_id, task_id, UNASSIGNED_ASSIGNEE, new_assignee):
                    raise _ReassignmentConflict(
                        f"Task {task_id} of job {job_id} changed "
                        f"concurrently during rebalancing")
                applied.append(
                    (job_id, task_id, UNASSIGNED_ASSIGNEE, new_assignee))

            _set_job_blocked(job_id, False)
            unblocked_job_ids.append(job_id)

            # Req 6.7: members holding zero tasks in the job before this
            # rebalance get exactly one notification.
            newly_assigned = sorted(
                {assignee for assignee in assignments.values()
                 if prior_counts.get(assignee, 0) == 0})
            if newly_assigned:
                notifications.append({
                    'action': 'notify_new_members',
                    'job_id': job_id,
                    'member_ids': newly_assigned,
                })
    except Exception as e:  # noqa: BLE001 — Req 5.7: full rollback
        logger.error(f"Member-addition rebalancing failed for team "
                     f"{team_id}: {e}", exc_info=True)
        _rollback_reassignments(applied)
        for job_id in unblocked_job_ids:
            try:
                _set_job_blocked(job_id, True)
            except Exception as reblock_error:  # noqa: BLE001
                logger.error(f"Could not restore blocked flag on job "
                             f"{job_id} during rollback: {reblock_error}")
        status_code = 409 if isinstance(e, _ReassignmentConflict) else 500
        return create_response(status_code, {
            'error': 'Team member was added, but assignment of the '
                     'blocked jobs\' tasks could not be completed; all '
                     'prior task assignments are unchanged',
            'reason': str(e),
            'team_id': team_id,
            'user_id': added_user_id,
        })

    # Notifications go out only once every blocked job rebalanced
    # (the worker resolves emails and applies the task 7.4 send path).
    for payload in notifications:
        _invoke_labeling_worker(payload)
    return None


class _ReassignmentConflict(Exception):
    """A conditional reassignment write lost a race (Req 5.7)."""


# ---------------------------------------------------------------------------
# Cognito / role helpers
# ---------------------------------------------------------------------------

def _cognito_attrs(attributes: List[Dict]) -> Dict[str, str]:
    return {attr['Name']: attr['Value'] for attr in attributes or []}


def _resolve_cognito_user(user_id: str) -> Optional[Dict]:
    """Resolve a portal user from Cognito by sub (the identity task
    assignments and ownership checks use) or by username.

    Returns {'sub', 'username', 'email', 'role'} or None when no such
    user exists in the User_Pool.
    """
    # 1. Lookup by sub (list_users filter) — team member user_ids are
    #    Cognito subs so task ownership checks can compare them to the
    #    caller's JWT sub directly.
    try:
        response = cognito_client.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'sub = "{user_id}"',
            Limit=1,
        )
        users = response.get('Users', [])
        if users:
            attrs = _cognito_attrs(users[0].get('Attributes'))
            return {
                'sub': attrs.get('sub', user_id),
                'username': users[0].get('Username', ''),
                'email': attrs.get('email', ''),
                'role': attrs.get('custom:role') or 'Viewer',
            }
    except ClientError as e:
        logger.warning(f"list_users by sub failed for {user_id}: {e}")

    # 2. Fall back to a username lookup (the user administration pages
    #    identify accounts by username).
    try:
        response = cognito_client.admin_get_user(
            UserPoolId=USER_POOL_ID, Username=user_id)
        attrs = _cognito_attrs(response.get('UserAttributes'))
        return {
            'sub': attrs.get('sub', user_id),
            'username': response.get('Username', user_id),
            'email': attrs.get('email', ''),
            'role': attrs.get('custom:role') or 'Viewer',
        }
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'UserNotFoundException':
            return None
        raise


def _holds_data_labeler_role(cognito_user: Dict, usecase_id: str) -> bool:
    """Whether the user holds the Data_Labeler role (Req 3.3, 3.4).

    Role sources mirror shared_utils.RBACManager.get_user_role /
    user_admin.py: the Cognito custom:role attribute, or a per-usecase
    UserRoles table row for the team's Use_Case.
    """
    if cognito_user.get('role') == 'DataLabeler':
        return True
    if not USER_ROLES_TABLE:
        return False
    try:
        user_roles_table = dynamodb.Table(USER_ROLES_TABLE)
        response = user_roles_table.get_item(
            Key={'user_id': cognito_user['sub'], 'usecase_id': usecase_id})
        return response.get('Item', {}).get('role') == 'DataLabeler'
    except ClientError as e:
        logger.warning(f"UserRoles lookup failed for "
                       f"{cognito_user['sub']}: {e}")
        return False


# ---------------------------------------------------------------------------
# Route handlers (Requirement 3.7: admin-only via labeling-teams:manage)
# ---------------------------------------------------------------------------

@rbac_check([Permission.MANAGE_LABELING_TEAMS])
def list_labeling_teams(event, context):
    """GET /labeling-teams?usecase_id=

    Teams scoped to the Use_Case, each with its name and current member
    list carrying user identity and email (Requirement 3.8).
    """
    try:
        params = event.get('queryStringParameters') or {}
        usecase_id = params.get('usecase_id')
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})

        teams = []
        for meta in _query_usecase_team_metas(usecase_id):
            members = [{
                'user_id': member.get('user_id'),
                'email': member.get('email', ''),
                'added_at': member.get('added_at'),
            } for member in _team_members(meta['team_id'])]
            teams.append({
                'team_id': meta['team_id'],
                'usecase_id': meta['usecase_id'],
                'team_name': meta.get('team_name', ''),
                'created_at': meta.get('created_at'),
                'created_by': meta.get('created_by'),
                'members': members,
                'member_count': len(members),
            })

        return create_response(200, {'teams': teams, 'count': len(teams)})

    except Exception as e:
        logger.error(f"Error listing labeling teams: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to list labeling teams'})


@rbac_check([Permission.MANAGE_LABELING_TEAMS])
def create_labeling_team(event, context):
    """POST /labeling-teams  body: {usecase_id, team_name}

    Name validation (Requirement 3.2): non-empty, at most 128
    characters, unique among the Use_Case's teams (usecase-teams-index
    query). Rejections identify the offending name and persist nothing.
    """
    try:
        user = get_user_from_event(event)
        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {'error': 'Request body is not valid JSON'})

        usecase_id = body.get('usecase_id')
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})

        raw_name = body.get('team_name')
        team_name = raw_name.strip() if isinstance(raw_name, str) else ''

        # Req 3.2: empty name
        if not team_name:
            return create_response(400, {
                'error': 'Team name must not be empty',
                'team_name': raw_name if isinstance(raw_name, str) else '',
            })

        # Req 3.2: name exceeding 128 characters
        if len(team_name) > TEAM_NAME_MAX_LENGTH:
            return create_response(400, {
                'error': f'Team name must be at most '
                         f'{TEAM_NAME_MAX_LENGTH} characters',
                'team_name': team_name,
            })

        # Req 3.2: duplicate name within the same Use_Case
        existing_names = {meta.get('team_name')
                          for meta in _query_usecase_team_metas(usecase_id)}
        if team_name in existing_names:
            return create_response(400, {
                'error': f"A labeling team named '{team_name}' already "
                         f"exists in this use case",
                'team_name': team_name,
            })

        # Req 3.1: persist the team scoped to the Use_Case
        team_id = f"team-{uuid.uuid4()}"
        timestamp = _now_ms()
        meta_item = {
            'team_id': team_id,
            'sk': TEAM_META_SK,
            'usecase_id': usecase_id,
            'team_name': team_name,
            'created_at': timestamp,
            'created_by': user['user_id'],
        }
        teams_table.put_item(Item=meta_item)

        log_audit_event(
            user_id=user['user_id'],
            action='create_labeling_team',
            resource_type='labeling_team',
            resource_id=team_id,
            result='success',
            details={'usecase_id': usecase_id, 'team_name': team_name},
        )

        team = {key: value for key, value in meta_item.items() if key != 'sk'}
        team['members'] = []
        team['member_count'] = 0
        return create_response(201, {
            'message': 'Labeling team created successfully',
            'team': team,
        })

    except Exception as e:
        logger.error(f"Error creating labeling team: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to create labeling team'})


@rbac_check([Permission.MANAGE_LABELING_TEAMS], allow_global=True)
def delete_labeling_team(event, context):
    """DELETE /labeling-teams/{teamId}

    Rejected while an InProgress labeling job references the team, so an
    assigned job never loses its team out from under it.
    """
    try:
        user = get_user_from_event(event)
        team_id = (event.get('pathParameters') or {}).get('teamId')

        meta = _get_team_meta(team_id)
        if not meta:
            return create_response(404, {'error': 'Labeling team not found'})

        blocking_jobs = _in_progress_jobs_referencing_team(
            meta['usecase_id'], team_id)
        if blocking_jobs:
            return create_response(409, {
                'error': 'Team cannot be deleted while referenced by an '
                         'in-progress labeling job',
                'in_progress_job_ids': [job['job_id']
                                        for job in blocking_jobs],
            })

        # Delete every item in the team partition (META + members).
        items = _query_team_items(team_id)
        with teams_table.batch_writer() as batch:
            for item in items:
                batch.delete_item(
                    Key={'team_id': item['team_id'], 'sk': item['sk']})

        log_audit_event(
            user_id=user['user_id'],
            action='delete_labeling_team',
            resource_type='labeling_team',
            resource_id=team_id,
            result='success',
            details={
                'usecase_id': meta['usecase_id'],
                'team_name': meta.get('team_name', ''),
            },
        )

        return create_response(200, {
            'message': 'Labeling team deleted successfully',
        })

    except Exception as e:
        logger.error(f"Error deleting labeling team: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to delete labeling team'})


@rbac_check([Permission.MANAGE_LABELING_TEAMS], allow_global=True)
def add_team_member(event, context):
    """POST /labeling-teams/{teamId}/members  body: {user_id}

    Validates the Data_Labeler role (Req 3.4) and duplicate membership
    (Req 3.5); persists the member with their portal account email
    (Req 3.3). Rejections leave the team's membership unchanged.
    """
    try:
        user = get_user_from_event(event)
        team_id = (event.get('pathParameters') or {}).get('teamId')

        meta = _get_team_meta(team_id)
        if not meta:
            return create_response(404, {'error': 'Labeling team not found'})

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {'error': 'Request body is not valid JSON'})

        requested_user_id = body.get('user_id')
        if not requested_user_id:
            return create_response(400, {'error': 'user_id is required'})

        cognito_user = _resolve_cognito_user(requested_user_id)
        if not cognito_user:
            return create_response(404, {
                'error': 'User not found',
                'user_id': requested_user_id,
            })

        # Req 3.4: only users holding the Data_Labeler role may be added.
        if not _holds_data_labeler_role(cognito_user, meta['usecase_id']):
            return create_response(400, {
                'error': 'User does not hold the Data_Labeler role',
                'user_id': requested_user_id,
                'required_role': 'DataLabeler',
            })

        member_user_id = cognito_user['sub']
        member_sk = f"{MEMBER_SK_PREFIX}{member_user_id}"

        # Req 3.5: duplicate membership is rejected, membership unchanged.
        existing = teams_table.get_item(
            Key={'team_id': team_id, 'sk': member_sk}).get('Item')
        if existing:
            return create_response(409, {
                'error': 'User is already a member of this labeling team',
                'user_id': member_user_id,
            })

        member_item = {
            'team_id': team_id,
            'sk': member_sk,
            'user_id': member_user_id,
            'email': cognito_user['email'],
            'added_at': _now_ms(),
            'added_by': user['user_id'],
        }
        # Conditional put: never silently overwrite a concurrent add.
        try:
            teams_table.put_item(
                Item=member_item,
                ConditionExpression='attribute_not_exists(team_id)',
            )
        except ClientError as e:
            if (e.response.get('Error', {}).get('Code')
                    == 'ConditionalCheckFailedException'):
                return create_response(409, {
                    'error': 'User is already a member of this labeling team',
                    'user_id': member_user_id,
                })
            raise

        log_audit_event(
            user_id=user['user_id'],
            action='add_labeling_team_member',
            resource_type='labeling_team',
            resource_id=team_id,
            result='success',
            details={
                'usecase_id': meta['usecase_id'],
                'member_user_id': member_user_id,
                'member_email': cognito_user['email'],
            },
        )

        # Task 7.2 (Req 5.5, 6.7): after persisting the membership,
        # distribute each blocked InProgress job's UNASSIGNED tasks
        # across the team's current Data_Labeler members and clear the
        # blocked indication; members who previously held zero tasks in
        # a job are notified via the worker. On partial failure all
        # prior assignments are restored (Req 5.7).
        error_response = _rebalance_blocked_jobs_for_member_addition(
            meta, team_id, member_user_id)
        if error_response:
            return error_response

        return create_response(201, {
            'message': 'Team member added successfully',
            'member': {
                'user_id': member_user_id,
                'email': cognito_user['email'],
                'added_at': member_item['added_at'],
            },
        })

    except Exception as e:
        logger.error(f"Error adding team member: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to add team member'})


@rbac_check([Permission.MANAGE_LABELING_TEAMS], allow_global=True)
def remove_team_member(event, context):
    """DELETE /labeling-teams/{teamId}/members/{userId}

    Persists the removal (Req 3.6). Reassignment of the member's
    unsubmitted Task_Assignments in InProgress jobs is wired in here by
    task 7.2 (Req 5.3, 5.4) — see the TODO hook point below.
    """
    try:
        user = get_user_from_event(event)
        path_params = event.get('pathParameters') or {}
        team_id = path_params.get('teamId')
        member_user_id = path_params.get('userId')

        meta = _get_team_meta(team_id)
        if not meta:
            return create_response(404, {'error': 'Labeling team not found'})

        member_sk = f"{MEMBER_SK_PREFIX}{member_user_id}"
        member = teams_table.get_item(
            Key={'team_id': team_id, 'sk': member_sk}).get('Item')
        if not member:
            return create_response(404, {
                'error': 'User is not a member of this labeling team',
                'user_id': member_user_id,
            })

        # Task 7.2: BEFORE deleting the membership, reassign this
        # member's unsubmitted Task_Assignments in the team's InProgress
        # jobs across the remaining Data_Labeler members (Req 5.3) — or,
        # when this is the last member, park them as UNASSIGNED and mark
        # the affected jobs blocked (Req 5.4). On any partial failure
        # the prior assignments are restored and the membership is left
        # unchanged (Req 5.7).
        member_user_id = member.get('user_id', member_user_id)
        error_response = _reassign_tasks_for_member_removal(
            meta, team_id, member_user_id)
        if error_response:
            return error_response

        # Membership is deleted only after all reassignments succeed
        # (Req 3.6: exclusion from subsequently created jobs'
        # distribution).
        teams_table.delete_item(Key={'team_id': team_id, 'sk': member_sk})

        log_audit_event(
            user_id=user['user_id'],
            action='remove_labeling_team_member',
            resource_type='labeling_team',
            resource_id=team_id,
            result='success',
            details={
                'usecase_id': meta['usecase_id'],
                'member_user_id': member_user_id,
                'member_email': member.get('email', ''),
            },
        )

        return create_response(200, {
            'message': 'Team member removed successfully',
        })

    except Exception as e:
        logger.error(f"Error removing team member: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to remove team member'})


# ---------------------------------------------------------------------------
# DDA job creation (task 5.3 — Requirements 4.1-4.11, 8.1, 8.8, 9.1-9.3,
# 11.3, 11.7, 12.1-12.3)
# ---------------------------------------------------------------------------

def _validation_error(parameter: str, message: str, **extra) -> Dict:
    """One entry in the validation_errors list (Req 4.9: identify each
    missing or invalid parameter)."""
    error = {'parameter': parameter, 'message': message}
    error.update(extra)
    return error


def _usecase_job_names(usecase_id: str) -> set:
    """Names of every Labeling_Job already stored for the Use_Case (name
    uniqueness, Req 4.1)."""
    names = set()
    kwargs: Dict[str, Any] = {
        'IndexName': 'usecase-jobs-index',
        'KeyConditionExpression': 'usecase_id = :uc',
        'ExpressionAttributeValues': {':uc': usecase_id},
        'ProjectionExpression': 'job_name',
    }
    while True:
        response = labeling_jobs_table.query(**kwargs)
        for item in response.get('Items', []):
            if item.get('job_name'):
                names.add(item['job_name'])
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return names


def _validate_label_set(raw_label_set: Any) -> tuple:
    """Validate a Segmentation/ObjectDetection Label_Set (Req 4.2):
    1-10 distinct, non-empty class names of at most 64 characters.

    Returns (label_set or None, [validation errors]).
    """
    errors: List[Dict] = []
    if not isinstance(raw_label_set, list) or not raw_label_set:
        errors.append(_validation_error(
            'label_set',
            f'A label set with between 1 and {LABEL_SET_MAX_CLASSES} '
            f'class names is required for this modality'))
        return None, errors

    if len(raw_label_set) > LABEL_SET_MAX_CLASSES:
        errors.append(_validation_error(
            'label_set',
            f'Label set must contain at most {LABEL_SET_MAX_CLASSES} '
            f'class names',
            label_count=len(raw_label_set)))

    seen = set()
    for index, raw_name in enumerate(raw_label_set):
        name = raw_name.strip() if isinstance(raw_name, str) else ''
        if not name:
            errors.append(_validation_error(
                'label_set',
                f'Label set entry {index} must be a non-empty class name'))
            continue
        if len(name) > LABEL_NAME_MAX_LENGTH:
            errors.append(_validation_error(
                'label_set',
                f"Class name '{name}' exceeds {LABEL_NAME_MAX_LENGTH} "
                f'characters',
                class_name=name))
        if name in seen:
            errors.append(_validation_error(
                'label_set',
                f"Class name '{name}' is duplicated in the label set",
                class_name=name))
        seen.add(name)

    if errors:
        return None, errors
    return [name.strip() for name in raw_label_set], errors


def _validate_example_refs(example_images: Any) -> tuple:
    """Validate the good/bad example image references (Req 4.4): at most
    10 of each, every reference a JPEG or PNG.

    Returns ({'good': [...], 'bad': [...]}, [validation errors]).
    """
    errors: List[Dict] = []
    if example_images is None:
        return {'good': [], 'bad': []}, errors
    if not isinstance(example_images, dict):
        errors.append(_validation_error(
            'example_images',
            "example_images must be an object of the form "
            "{'good': [...], 'bad': [...]}"))
        return {'good': [], 'bad': []}, errors

    validated = {}
    for kind in ('good', 'bad'):
        refs = example_images.get(kind) or []
        if not isinstance(refs, list):
            errors.append(_validation_error(
                'example_images',
                f'{kind} example images must be a list of image '
                f'references'))
            validated[kind] = []
            continue
        if len(refs) > EXAMPLE_IMAGES_MAX:
            errors.append(_validation_error(
                'example_images',
                f'At most {EXAMPLE_IMAGES_MAX} {kind} example images are '
                f'allowed',
                example_count=len(refs)))
        for ref in refs:
            if (not isinstance(ref, str)
                    or not ref.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS)):
                errors.append(_validation_error(
                    'example_images',
                    f"{kind} example image '{ref}' is not a JPEG or PNG "
                    f'image',
                    example_ref=ref if isinstance(ref, str) else str(ref)))
        validated[kind] = [ref for ref in refs if isinstance(ref, str)]
    return validated, errors


def _few_shot_examples_from_refs(
        example_images: Dict[str, List[str]]) -> List[Dict]:
    """Derive the ordered Few_Shot_Example set from the job's uploaded
    example image references (llm-autolabel-prompt-tuning Req 6.4).

    The derived list is a copy: good example references in stored (wizard
    upload) order first, then bad example references in their stored
    order, each carrying its good-or-bad designation and its position
    *within that designation*. `example_images` itself is never
    modified — it keeps its labeler-instruction role untouched
    (Req 10.6).
    """
    examples: List[Dict] = []
    for designation in (FEW_SHOT_GOOD, FEW_SHOT_BAD):
        for position, ref in enumerate(example_images.get(designation) or []):
            examples.append({
                'ref': ref,
                'designation': designation,
                'position': position,
            })
    return examples


def _resolve_few_shot_document(auto_label: Dict, body: Dict,
                               example_images: Dict) -> tuple:
    """The `auto_label.few_shot` sub-document to persist for an `llm:`
    job, plus any validation errors (llm-autolabel-prompt-tuning
    Req 6.2, 6.3, 6.4, 10.4, 10.6).

    Returns (document or None, [validation errors]). `None` means write
    no `few_shot` key at all — the outcome for a submission that carries
    no Few_Shot_Option, so a pre-feature submission produces a
    byte-identical job record (Req 10.4) and the Auto_Labeler's
    "absent means disabled" contract covers it (Req 10.3).
    """
    raw = auto_label.get('few_shot')
    if raw is None:
        # The wizard nests the option under auto_label; accept a
        # top-level key too so either shape persists identically.
        raw = body.get('few_shot')
    if raw is None:
        return None, []

    if isinstance(raw, bool):
        enabled = raw
    elif isinstance(raw, dict):
        enabled = bool(raw.get('enabled'))
    else:
        return None, [_validation_error(
            'few_shot',
            "few_shot must be an object like {'enabled': true}")]

    if not enabled:
        return {'enabled': False}, []

    # Req 6.2/6.3: the option is meaningless without at least one
    # example image, and the error joins the enumerated list.
    examples = _few_shot_examples_from_refs(example_images)
    if not examples:
        return None, [_validation_error(
            'few_shot',
            'At least one example image is required for the few-shot '
            'examples option')]
    return {'enabled': True, 'examples': examples}, []


def _team_data_labeler_members(team_id: str, usecase_id: str) -> List[Dict]:
    """Team members currently holding the Data_Labeler role (Req 4.8 —
    roles are re-resolved at job creation, not trusted from add time)."""
    labelers = []
    for member in _team_members(team_id):
        try:
            cognito_user = _resolve_cognito_user(member['user_id'])
        except Exception as e:  # noqa: BLE001 — a broken account is not a labeler
            logger.warning(f"Could not resolve team member "
                           f"{member.get('user_id')}: {e}")
            cognito_user = None
        if cognito_user and _holds_data_labeler_role(cognito_user, usecase_id):
            labelers.append(member)
    return labelers


def _enumerate_dataset_images(s3_client, bucket: str, prefix: str) -> tuple:
    """Enumerate every object under the dataset prefix, nested prefixes
    included (Req 4.5).

    Objects that are not supported images are skipped rather than
    rejected (Req 4.7): datasets routinely carry sidecar files such as
    manifests next to (or under) the images, and those must not block
    job creation. Returns (image keys, skipped objects) where each
    skipped entry identifies the key and why it was skipped, for
    reporting and observability.
    """
    images: List[str] = []
    skipped: List[Dict] = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('/'):
                continue  # folder placeholder objects carry no image data
            if key.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
                images.append(key)
            else:
                skipped.append({'key': key, 'reason': 'unsupported_format'})
    return images, skipped


def _invoke_labeling_worker(payload: Dict) -> None:
    """Fire-and-forget async invoke of dda_labeling_worker. Guarded so
    environments without the worker wired (tests) still create jobs."""
    function_name = os.environ.get('DDA_LABELING_WORKER_FUNCTION_NAME')
    if not function_name:
        logger.warning('DDA_LABELING_WORKER_FUNCTION_NAME is not set; '
                       'skipping async worker invocation')
        return
    try:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=json.dumps(payload),
        )
    except Exception as e:  # noqa: BLE001 — the job is already persisted
        logger.error(f"Failed to async-invoke dda_labeling_worker "
                     f"({function_name}): {e}")


def create_dda_job(body: Dict, user: Dict, event: Optional[Dict] = None):
    """Create a DDA Labeling_Job (called from labeling.py's backend
    switch with the request body and the authenticated user).

    All parameters are validated before the dataset prefix is enumerated
    (Req 4.5); any rejection enumerates each offending element and
    persists nothing (Req 4.9, 4.10). On success the job is persisted
    with status InProgress (Req 4.11, 11.3), a job_created audit event
    is written (Req 11.7), and dda_labeling_worker is async-invoked with
    {action: 'distribute', job_id}.

    The `grounded-sam` auto-label family (spec grounded-sam-autolabel)
    is accepted for Segmentation/ObjectDetection with optional per-label
    Prompt_Overrides persisted under `auto_label.prompt_overrides`
    (that spec's Req 1.5-1.7, 2.4-2.6, 2.8); other families' records
    never carry the key.
    """
    try:
        errors: List[Dict] = []

        usecase_id = body.get('usecase_id')
        if not usecase_id:
            errors.append(_validation_error(
                'usecase_id', 'usecase_id is required'))

        # --- job name: 1-63 chars, unique per Use_Case (Req 4.1) ---
        raw_name = body.get('job_name')
        job_name = raw_name.strip() if isinstance(raw_name, str) else ''
        if not job_name or len(job_name) > JOB_NAME_MAX_LENGTH:
            errors.append(_validation_error(
                'job_name',
                f'Job name must be between 1 and {JOB_NAME_MAX_LENGTH} '
                f'characters',
                job_name=raw_name if isinstance(raw_name, str) else ''))
        elif usecase_id and job_name in _usecase_job_names(usecase_id):
            errors.append(_validation_error(
                'job_name',
                f"A labeling job named '{job_name}' already exists in "
                f'this use case',
                job_name=job_name))

        # --- modality (Req 4.1) ---
        modality = body.get('task_type') or body.get('modality')
        if modality not in VALID_MODALITIES:
            errors.append(_validation_error(
                'task_type',
                f"Labeling modality must be one of "
                f"{', '.join(VALID_MODALITIES)}",
                task_type=modality))

        # --- Label_Set (Req 4.2, 4.3) ---
        if modality == 'Classification':
            # Fixed label set for Binary_Classification regardless of input.
            label_set: Optional[List[str]] = list(CLASSIFICATION_LABEL_SET)
        elif modality in VALID_MODALITIES:
            raw_label_set = body.get('label_set', body.get('label_categories'))
            label_set, label_errors = _validate_label_set(raw_label_set)
            errors.extend(label_errors)
        else:
            label_set = None

        skip_verification = bool(body.get('skip_verification'))

        # --- Skip_Verification_Mode (Req 9.1-9.3) ---
        bedrock_model_id = None
        per_label_prompts: Dict[str, str] = {}
        if skip_verification:
            # Req 9.1: admin authorization, rejected with an
            # authorization error (and audit event) otherwise.
            role = rbac_manager.get_user_role(
                user['user_id'], usecase_id or 'global', user)
            role_value = role.value if role else None
            if role_value not in SKIP_VERIFICATION_ADMIN_ROLES:
                log_audit_event(
                    user_id=user['user_id'],
                    action='unauthorized_access',
                    resource_type='labeling_job',
                    resource_id=job_name or 'unknown',
                    result='denied',
                    details={
                        'usecase_id': usecase_id or '',
                        'reason': 'skip_verification requires administrator '
                                  'authorization',
                    },
                )
                return create_response(403, {
                    'error': 'Skip-verification mode requires administrator '
                             'authorization',
                })

            # Req 9.2/9.3: Bedrock model + non-empty Per_Label_Prompts
            # covering every label; each missing/empty item identified.
            bedrock_model_id = body.get('bedrock_model_id')
            if not bedrock_model_id:
                errors.append(_validation_error(
                    'bedrock_model_id',
                    'A Bedrock model selection is required for '
                    'skip-verification mode'))
            raw_prompts = body.get('per_label_prompts')
            if raw_prompts is not None and not isinstance(raw_prompts, dict):
                errors.append(_validation_error(
                    'per_label_prompts',
                    'per_label_prompts must map each label to a prompt'))
                raw_prompts = {}
            raw_prompts = raw_prompts or {}
            if label_set:
                for label in label_set:
                    prompt = raw_prompts.get(label)
                    if not isinstance(prompt, str) or not prompt.strip():
                        errors.append(_validation_error(
                            'per_label_prompts',
                            f"A non-empty prompt is required for label "
                            f"'{label}'",
                            label=label))
                    else:
                        per_label_prompts[label] = prompt
        # --- Labeling_Team (Req 4.1, 4.8: required unless skip) ---
        team_id = body.get('team_id') if not skip_verification else None
        if not skip_verification:
            if not team_id:
                errors.append(_validation_error(
                    'team_id',
                    'A labeling team is required unless skip-verification '
                    'mode is enabled'))
            else:
                team_meta = _get_team_meta(team_id)
                if not team_meta:
                    errors.append(_validation_error(
                        'team_id', 'Labeling team not found',
                        team_id=team_id))
                elif usecase_id and team_meta['usecase_id'] != usecase_id:
                    errors.append(_validation_error(
                        'team_id',
                        'Labeling team belongs to a different use case',
                        team_id=team_id))
                elif not _team_data_labeler_members(
                        team_id, team_meta['usecase_id']):
                    errors.append(_validation_error(
                        'team_id',
                        f"Labeling team "
                        f"'{team_meta.get('team_name', team_id)}' has no "
                        f'members with the Data_Labeler role',
                        team_id=team_id))

        # --- instructions (Req 4.4) ---
        instructions = body.get('instructions') or ''
        if not isinstance(instructions, str):
            errors.append(_validation_error(
                'instructions', 'Instructions must be text'))
            instructions = ''
        elif len(instructions) > INSTRUCTIONS_MAX_LENGTH:
            errors.append(_validation_error(
                'instructions',
                f'Instructions must be at most {INSTRUCTIONS_MAX_LENGTH} '
                f'characters',
                instructions_length=len(instructions)))

        # --- example images (Req 4.4) ---
        example_images, example_errors = _validate_example_refs(
            body.get('example_images'))
        errors.extend(example_errors)

        # --- auto-label model/modality matrix (Req 8.1, 8.8) ---
        auto_label = body.get('auto_label') or {}
        if not isinstance(auto_label, dict):
            errors.append(_validation_error(
                'auto_label', "auto_label must be an object like "
                              "{'enabled': true, 'model': 'sam'}"))
            auto_label = {}
        auto_label_enabled = bool(auto_label.get('enabled'))
        auto_label_model = auto_label.get('model')
        model_family = None
        detection_prompt = None
        prompt_overrides: Dict[str, str] = {}
        if auto_label_enabled:
            if auto_label_model == 'sam':
                model_family = 'sam'
            elif auto_label_model == 'grounded-sam':
                # grounded-sam-autolabel Requirement 1.5: exact-match
                # family value, judged compatible with Segmentation and
                # ObjectDetection through the matrix check below (1.6).
                model_family = 'grounded-sam'
                # grounded-sam-autolabel Requirements 2.4-2.6: optional
                # per-label Prompt_Overrides. Accepted absent; when
                # present the value must be an object whose keys belong
                # to the submitted Label_Set and whose values are
                # strings of raw length <= PROMPT_OVERRIDE_MAX_LENGTH.
                # Values empty after trimming are dropped; survivors are
                # kept character-for-character (Req 2.4).
                raw_overrides = auto_label.get('prompt_overrides')
                if raw_overrides is not None and not isinstance(
                        raw_overrides, dict):
                    errors.append(_validation_error(
                        'auto_label',
                        'prompt_overrides must be an object mapping '
                        'label names to prompt strings'))
                    raw_overrides = None
                for key, value in (raw_overrides or {}).items():
                    if key not in (label_set or []):
                        errors.append(_validation_error(
                            'auto_label',
                            f"prompt_overrides key '{key}' is not a "
                            f"label of this job's label set",
                            label=key))
                    elif not isinstance(value, str):
                        errors.append(_validation_error(
                            'auto_label',
                            f"The prompt override for label '{key}' "
                            f'must be text',
                            label=key))
                    elif len(value) > PROMPT_OVERRIDE_MAX_LENGTH:
                        errors.append(_validation_error(
                            'auto_label',
                            f"The prompt override for label '{key}' "
                            f'must be at most '
                            f'{PROMPT_OVERRIDE_MAX_LENGTH} characters',
                            label=key))
                    elif value.strip():
                        prompt_overrides[key] = value
            elif (isinstance(auto_label_model, str)
                    and auto_label_model.startswith('bedrock:')
                    and auto_label_model.split(':', 1)[1]):
                model_family = 'bedrock'
            elif (isinstance(auto_label_model, str)
                    and auto_label_model.startswith('llm:')):
                # llm-auto-labeling Requirement 1.5: split on the first
                # colon only — model identifiers legitimately contain
                # colons (e.g. 'us.amazon.nova-pro-v1:0').
                model_family = 'llm'
                identifier_error = validate_model_identifier(
                    auto_label_model.split(':', 1)[1])
                if identifier_error:
                    errors.append(_validation_error(
                        'auto_label',
                        f'Auto-label {identifier_error}',
                        model=auto_label_model))
                # llm-auto-labeling Requirement 2: Detection_Prompt is
                # required (emptiness judged on the stripped value) and
                # at most DETECTION_PROMPT_MAX_LENGTH characters
                # (length judged on the raw value). The raw string is
                # what gets persisted (Req 2.5: character-for-character).
                raw_prompt = auto_label.get('detection_prompt')
                if not isinstance(raw_prompt, str) or not raw_prompt.strip():
                    errors.append(_validation_error(
                        'auto_label',
                        'A non-empty detection_prompt is required for '
                        'LLM auto-labeling'))
                elif len(raw_prompt) > DETECTION_PROMPT_MAX_LENGTH:
                    errors.append(_validation_error(
                        'auto_label',
                        f'detection_prompt must be at most '
                        f'{DETECTION_PROMPT_MAX_LENGTH} characters',
                        detection_prompt_length=len(raw_prompt)))
                else:
                    detection_prompt = raw_prompt
            else:
                # grounded-sam-autolabel: grounded-sam is PREPENDED so
                # the substring "'sam' or 'bedrock:<model_id>'" pinned
                # by test_dda_labeling_create_job.py stays intact.
                errors.append(_validation_error(
                    'auto_label',
                    f"Auto-label model must be 'grounded-sam', "
                    f"'sam' or 'bedrock:<model_id>'",
                    model=auto_label_model))
            if (model_family and modality in VALID_MODALITIES
                    and modality not in
                    AUTO_LABEL_MODEL_MODALITIES[model_family]):
                errors.append(_validation_error(
                    'auto_label',
                    f"Auto-label model '{auto_label_model}' does not "
                    f"support the {modality} modality",
                    model=auto_label_model,
                    task_type=modality))

        # --- Few_Shot_Option (llm-autolabel-prompt-tuning Req 6.2-6.4,
        #     10.1, 10.4, 10.6) ---
        # Scoped to the `llm:` family: sam / bedrock: jobs get no
        # few_shot key at all, so their records and request construction
        # are untouched (Req 10.1).
        few_shot_document = None
        if model_family == 'llm':
            few_shot_document, few_shot_errors = _resolve_few_shot_document(
                auto_label, body, example_images)
            errors.extend(few_shot_errors)

        # --- Downscale_Setting + Token_Budget_Selection (llm-model-
        #     token-and-image-sizing Req 3.6, 5.7, 10.4, 10.6) ---
        # Scoped to the `llm:` family exactly as few_shot is: sam /
        # bedrock: submissions get neither key regardless of what they
        # carried (Req 10.4), and a submission omitting both yields a
        # record byte-identical to a pre-feature record with no
        # validation message mentioning either value (Req 10.6).
        # The record holds one representation only — the attribute is
        # absent for Downscale_Off, an option integer otherwise —
        # so a submitted null (the wizard's blank select) drops the key,
        # and `normalize_downscale_setting`'s totality means a malformed
        # value degrades to Downscale_Off rather than into the record.
        # The Token_Budget_Selection is persisted unchanged exactly when
        # it is what Req 3.6 persists: a non-boolean integer in
        # [1, MODEL_TOKEN_LIMIT_CEILING]. An empty budget control omits
        # the key, so the Auto_Labeler resolves through the
        # Model_Token_Limits and the default (Req 3.8, 3.10).
        downscale_max_edge = None
        token_budget = None
        if model_family == 'llm':
            downscale_max_edge = normalize_downscale_setting(
                auto_label.get('downscale_max_edge'))
            raw_budget = auto_label.get('token_budget')
            if (isinstance(raw_budget, int)
                    and not isinstance(raw_budget, bool)
                    and 1 <= raw_budget <= MODEL_TOKEN_LIMIT_CEILING):
                token_budget = raw_budget

        # --- dataset prefix + use case (needed for enumeration) ---
        dataset_prefix = body.get('dataset_prefix')
        if not dataset_prefix:
            errors.append(_validation_error(
                'dataset_prefix', 'dataset_prefix is required'))

        usecase = None
        dataset_bucket = None
        if usecase_id:
            try:
                usecase = get_usecase(usecase_id)
            except ValueError:
                errors.append(_validation_error(
                    'usecase_id', 'Use case not found',
                    usecase_id=usecase_id))
            if usecase:
                dataset_bucket = (usecase.get('data_s3_bucket')
                                  or usecase.get('s3_bucket'))
                if not dataset_bucket:
                    errors.append(_validation_error(
                        'usecase_id',
                        'Use case has no data bucket configured'))

        # Req 4.9/4.10: reject before touching S3 or persisting anything.
        if errors:
            return create_response(400, {
                'error': 'Labeling job validation failed',
                'validation_errors': errors,
            })

        # --- dataset enumeration (Req 4.5-4.7, 12.1-12.3) ---
        try:
            s3_client = get_s3_client_for_bucket(
                usecase, dataset_bucket, 'dda-labeling-create')
            images, skipped_objects = _enumerate_dataset_images(
                s3_client, dataset_bucket, dataset_prefix)
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            logger.error(f"Dataset enumeration failed for "
                         f"s3://{dataset_bucket}/{dataset_prefix}: {e}")
            return create_response(400, {
                'error': f"Dataset location "
                         f"s3://{dataset_bucket}/{dataset_prefix} is not "
                         f'accessible',
                'dataset_bucket': dataset_bucket,
                'dataset_prefix': dataset_prefix,
                'reason': error_code,
            })

        # Req 4.7: non-image objects are skipped, not rejected. Log them so
        # an unexpectedly low image count can be explained after the fact.
        if skipped_objects:
            logger.info(
                f"Skipped {len(skipped_objects)} non-image object(s) under "
                f"s3://{dataset_bucket}/{dataset_prefix}: "
                f"{[o['key'] for o in skipped_objects[:10]]}"
                f"{' ...' if len(skipped_objects) > 10 else ''}")

        if not images:
            # Req 4.6. When the prefix held only skipped objects, say so —
            # otherwise "no images found" reads as an empty prefix.
            return create_response(400, {
                'error': f"No image objects found under dataset prefix "
                         f"'{dataset_prefix}'"
                         + (f" ({len(skipped_objects)} object(s) under the "
                            f"prefix are not JPEG or PNG images)"
                            if skipped_objects else ''),
                'dataset_bucket': dataset_bucket,
                'dataset_prefix': dataset_prefix,
                'skipped_object_count': len(skipped_objects),
            })

        # --- persistence (Req 4.11, 11.3, 12.8) ---
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        now = int(datetime.utcnow().timestamp())
        job_item: Dict[str, Any] = {
            'job_id': job_id,
            'usecase_id': usecase_id,
            'job_name': job_name,
            'labeling_backend': 'DDA',
            'status': 'InProgress',
            'task_type': modality,
            'label_set': label_set,
            'dataset_prefix': dataset_prefix,
            'dataset_bucket': dataset_bucket,
            'image_count': len(images),
            # Req 4.7: non-image objects under the prefix are skipped;
            # recording the count keeps image_count explainable.
            'skipped_object_count': len(skipped_objects),
            'instructions': instructions,
            'example_images': example_images,
            'auto_label': {
                'enabled': auto_label_enabled,
                **({'model': auto_label_model} if auto_label_enabled else {}),
                # llm-auto-labeling Req 2.5: the raw prompt, stored
                # character-for-character as entered.
                **({'detection_prompt': detection_prompt}
                   if model_family == 'llm' else {}),
                # llm-autolabel-prompt-tuning Req 6.4/10.6: the
                # Few_Shot_Option and, when enabled, the derived example
                # set in attachment order. Absent for sam / bedrock:
                # jobs and for submissions that carry no option at all.
                **({'few_shot': few_shot_document}
                   if few_shot_document is not None else {}),
                # llm-model-token-and-image-sizing Req 5.7/3.6: the
                # submitted Downscale_Setting and Token_Budget_Selection,
                # unchanged, `llm:` family only. Both attributes are
                # absent for Downscale_Off / an omitted budget, so an
                # unconfigured submission's record is byte-identical to
                # a pre-feature record (Req 10.6) and sam / bedrock:
                # records never carry either (Req 10.4).
                **({'downscale_max_edge': downscale_max_edge}
                   if downscale_max_edge is not None else {}),
                **({'token_budget': token_budget}
                   if token_budget is not None else {}),
                # grounded-sam-autolabel Req 2.4/2.8: the surviving
                # Prompt_Overrides, character-for-character, grounded-sam
                # family only. The key is absent for every other family
                # and for override-free grounded-sam jobs, so records of
                # the other families stay byte-identical to pre-feature
                # records (Req 7.1).
                **({'prompt_overrides': prompt_overrides}
                   if model_family == 'grounded-sam' and prompt_overrides
                   else {}),
            },
            'skip_verification': skip_verification,
            'submitted_count': 0,
            'blocked': False,
            'created_at': now,
            'updated_at': now,
            'created_by': user['user_id'],
        }
        if team_id:
            job_item['team_id'] = team_id
        if skip_verification:
            job_item['bedrock_model_id'] = bedrock_model_id
            job_item['per_label_prompts'] = per_label_prompts

        labeling_jobs_table.put_item(Item=job_item)

        # Req 11.7: job lifecycle audit event.
        log_audit_event(
            user_id=user['user_id'],
            action='job_created',
            resource_type='labeling_job',
            resource_id=job_id,
            result='success',
            details={
                'usecase_id': usecase_id,
                'job_name': job_name,
                'labeling_backend': 'DDA',
                'task_type': modality,
                'image_count': len(images),
                'skip_verification': skip_verification,
                # llm-auto-labeling Req 9.4: model identifier (absent
                # when auto-labeling is off) and auto-label mode.
                **({'auto_label_model': auto_label_model}
                   if auto_label_enabled else {}),
                'auto_label_mode': model_family if auto_label_enabled
                                   else 'none',
            },
        )

        # Distribution (and skip-verification fan-out) runs async in the
        # worker so job creation returns immediately.
        _invoke_labeling_worker({'action': 'distribute', 'job_id': job_id})

        return create_response(201, {
            'job_id': job_id,
            'status': 'InProgress',
            'labeling_backend': 'DDA',
            'image_count': len(images),
            # Req 4.7: reported so the caller can reconcile image_count
            # against the object count under the prefix.
            'skipped_object_count': len(skipped_objects),
            'message': 'DDA labeling job created successfully',
        })

    except Exception as e:
        logger.error(f"Error creating DDA labeling job: {str(e)}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Failed to create DDA labeling job'})


# ---------------------------------------------------------------------------
# Labeler read APIs (task 8.1 — Requirements 2.4, 2.6, 7.1, 7.2, 7.10,
# 7.11, 8.3, 8.6, 8.7, 12.6, 12.7)
# ---------------------------------------------------------------------------
# Authorization model: every labeler route carries
# @rbac_check([LABELING_TASKS_SELF], allow_global=True). The
# Data_Labeler role grants labeling:tasks-self globally through the
# Cognito custom:role claim (shared_utils role resolution step 4), so
# the permission gate needs no Use_Case scope — the authority over what
# a labeler may actually see is the server-side ownership check below:
# the Task_Assignment's assignee_user_id must equal the caller's sub
# AND the caller must currently be a member of the job's Labeling_Team
# (Req 2.4). Violations answer 403 carrying no resource data plus a
# labeler_access_denied audit event (Req 2.6).


def _parse_s3_uri(uri: str) -> tuple:
    """('bucket', 'key') from an s3://bucket/key URI."""
    remainder = uri[len('s3://'):] if uri.startswith('s3://') else uri
    bucket, _, key = remainder.partition('/')
    return bucket, key


def _is_current_team_member(team_id: Optional[str], user_sub: str) -> bool:
    """Req 2.4: membership is re-checked on every request, so a labeler
    removed from the team stops being served immediately."""
    if not team_id:
        return False
    item = teams_table.get_item(
        Key={'team_id': team_id,
             'sk': f'{MEMBER_SK_PREFIX}{user_sub}'}).get('Item')
    return item is not None


def _labeler_access_denied(user: Dict, resource_type: str,
                           resource_id: Optional[str], reason: str,
                           usecase_id: str = '') -> Dict:
    """Req 2.6: 403 with none of the requested resource data, plus a
    labeler_access_denied audit event with the caller's identity, the
    requested resource, and a timestamp (written by log_audit_event)."""
    log_audit_event(
        user_id=user['user_id'],
        action='labeler_access_denied',
        resource_type=resource_type,
        resource_id=resource_id or 'unknown',
        result='denied',
        details={'reason': reason, 'usecase_id': usecase_id},
    )
    return create_response(403, {'error': 'Access denied'})


def _query_caller_tasks(user_sub: str,
                        job_id: Optional[str] = None) -> List[Dict]:
    """The caller's Task_Assignments from the assignee-index GSI
    (assignee_user_id PK, job_id SK) — inherently scoped to the caller,
    optionally narrowed to one job."""
    key_condition = 'assignee_user_id = :assignee'
    values: Dict[str, Any] = {':assignee': user_sub}
    if job_id:
        key_condition += ' AND job_id = :jid'
        values[':jid'] = job_id
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'IndexName': 'assignee-index',
        'KeyConditionExpression': key_condition,
        'ExpressionAttributeValues': values,
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def _task_progress_counts(tasks: List[Dict]) -> tuple:
    """(submitted, remaining, withheld) over the caller's tasks in one
    job (Req 7.10/7.11): submitted = Submitted, remaining = unsubmitted
    Assigned, withheld = PresentationFailed. Inactive tasks (failed
    distribution) count nowhere."""
    submitted = sum(1 for task in tasks if task.get('status') == 'Submitted')
    remaining = sum(1 for task in tasks if task.get('status') == 'Assigned')
    withheld = sum(1 for task in tasks
                   if task.get('status') == 'PresentationFailed')
    return submitted, remaining, withheld


def _is_task_presentable(task: Dict) -> bool:
    """Presentation gating (Req 7.12, 8.6, 8.7): unsubmitted (status
    Assigned — PresentationFailed and Inactive are excluded by the
    status check) and Pre_Label generation not still Pending
    (None/absent, Available, and Failed are all presentable)."""
    if task.get('status') != 'Assigned':
        return False
    return (task.get('prelabel_status') or 'None') != PENDING_PRELABEL_STATUS


def _presign_task_image(job: Dict, task: Dict) -> tuple:
    """(url, expires_at): a read-only presigned GET URL scoped to
    exactly the task's image object, valid 15 minutes (Req 12.6),
    through the use case's cross-account access with the single-account
    direct fallback (get_s3_client_for_bucket, Req 12.1-12.3)."""
    usecase = get_usecase(job['usecase_id'])
    bucket, key = _parse_s3_uri(task['image_s3_uri'])
    dataset_s3 = get_s3_client_for_bucket(
        usecase, bucket, 'dda-labeling-labeler')
    url = dataset_s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=IMAGE_URL_EXPIRY_SECONDS,
    )
    expires_at = (int(datetime.utcnow().timestamp())
                  + IMAGE_URL_EXPIRY_SECONDS)
    return url, expires_at


def _load_prelabel(task: Dict) -> Optional[Any]:
    """The Pre_Label payload from prelabel_s3_key in the portal
    artifacts bucket (Req 8.3), or None when it cannot be read — the
    image is then presented without a pre-label rather than blocked."""
    key = task.get('prelabel_s3_key')
    if not key or not PORTAL_ARTIFACTS_BUCKET:
        return None
    try:
        response = s3_client.get_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET, Key=key)
        return json.loads(response['Body'].read())
    except Exception as e:  # noqa: BLE001 — degrade to no pre-label
        logger.warning(f"Could not load pre-label {key}: {e}")
        return None


def _example_image_urls(job: Dict) -> Dict[str, List[str]]:
    """Presigned GET URLs for the job's stored good/bad example images
    (Req 7.2). Kinds with no stored references are omitted, so a job
    without examples presents without them. References are portal
    artifacts bucket keys (the wizard's presigned-PUT uploads) or full
    s3:// URIs."""
    example_images = job.get('example_images') or {}
    urls: Dict[str, List[str]] = {}
    for kind in ('good', 'bad'):
        kind_urls: List[str] = []
        for ref in example_images.get(kind) or []:
            if not isinstance(ref, str) or not ref:
                continue
            if ref.startswith('s3://'):
                bucket, key = _parse_s3_uri(ref)
            else:
                bucket, key = PORTAL_ARTIFACTS_BUCKET, ref
            if not bucket:
                continue
            try:
                kind_urls.append(s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket, 'Key': key},
                    ExpiresIn=IMAGE_URL_EXPIRY_SECONDS,
                ))
            except Exception as e:  # noqa: BLE001 — omit the broken ref
                logger.warning(f"Could not presign example image "
                               f"{ref}: {e}")
        if kind_urls:
            urls[kind] = kind_urls
    return urls


@rbac_check([Permission.LABELING_TASKS_SELF], allow_global=True)
def list_labeler_jobs(event, context):
    """GET /labeler/jobs

    The InProgress DDA jobs in which the caller currently holds at
    least one unsubmitted (status=Assigned) Task_Assignment, found via
    the assignee-index, each with the caller's submitted/remaining/
    withheld counts (Req 7.10). Only jobs of teams the caller is a
    current member of are returned (Req 2.4); the list is empty when no
    such Task_Assignments exist.
    """
    try:
        user = get_user_from_event(event)
        caller = user['user_id']

        tasks_by_job: Dict[str, List[Dict]] = {}
        for task in _query_caller_tasks(caller):
            tasks_by_job.setdefault(task['job_id'], []).append(task)

        jobs_payload: List[Dict] = []
        for job_id in sorted(tasks_by_job):
            submitted, remaining, withheld = _task_progress_counts(
                tasks_by_job[job_id])
            if remaining == 0:
                continue  # no unsubmitted task held here (Req 2.4)
            job = labeling_jobs_table.get_item(
                Key={'job_id': job_id}).get('Item')
            if not job or job.get('labeling_backend') != 'DDA':
                continue
            if job.get('status') != 'InProgress':
                continue
            # Req 2.4: the caller must currently be a member of the
            # job's team — stale assignments of removed members are
            # never served.
            if not _is_current_team_member(job.get('team_id'), caller):
                continue
            jobs_payload.append({
                'job_id': job_id,
                'job_name': job.get('job_name', ''),
                'task_type': job.get('task_type'),
                'label_set': job.get('label_set'),
                'submitted_count': submitted,
                'remaining_count': remaining,
                'withheld_count': withheld,
            })

        return create_response(200, {
            'jobs': jobs_payload,
            'count': len(jobs_payload),
        })

    except Exception as e:
        logger.error(f"Error listing labeler jobs: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to list labeler jobs'})


@rbac_check([Permission.LABELING_TASKS_SELF], allow_global=True)
def get_next_labeler_task(event, context):
    """GET /labeler/jobs/{jobId}/next

    The caller's next presentable unsubmitted Task_Assignment in the
    job (Req 7.1): lowest task_id with status=Assigned whose Pre_Label
    is not still Pending (Req 7.12, 8.6, 8.7). The payload carries a
    15-minute presigned image URL (Req 12.6), the Pre_Label when
    available (Req 8.3), the job's instructions and example-image URLs
    omitting absent ones (Req 7.2), the modality and Label_Set, and the
    caller's counts (Req 7.10). When zero presentable unsubmitted tasks
    remain, a completion payload with the submitted and withheld counts
    is returned instead (Req 7.11).
    """
    try:
        user = get_user_from_event(event)
        caller = user['user_id']
        job_id = (event.get('pathParameters') or {}).get('jobId')

        job = (labeling_jobs_table.get_item(Key={'job_id': job_id})
               .get('Item') if job_id else None)
        # A missing job, a Ground Truth job, and another team's job are
        # indistinguishable to the caller: 403 with no data (Req 2.6).
        if not job or job.get('labeling_backend') != 'DDA':
            return _labeler_access_denied(
                user, 'labeling_job', job_id, 'job_not_accessible')
        if not _is_current_team_member(job.get('team_id'), caller):
            return _labeler_access_denied(
                user, 'labeling_job', job_id,
                'caller_not_current_team_member',
                job.get('usecase_id', ''))

        tasks = _query_caller_tasks(caller, job_id)
        if not tasks:
            # Req 2.6: a job in which the caller holds no
            # Task_Assignment is not theirs to read.
            return _labeler_access_denied(
                user, 'labeling_job', job_id,
                'no_tasks_assigned_to_caller', job.get('usecase_id', ''))

        if job.get('status') != 'InProgress':
            return create_response(409, {
                'error': 'Labeling job is not in progress',
                'job_id': job_id,
                'status': job.get('status'),
            })

        submitted, remaining, withheld = _task_progress_counts(tasks)
        presentable = sorted(
            (task for task in tasks if _is_task_presentable(task)),
            key=lambda task: task['task_id'])

        if not presentable:
            # Req 7.11: completion payload with the labeler's submitted
            # count and the count of withheld Task_Assignments.
            return create_response(200, {
                'complete': True,
                'job_id': job_id,
                'submitted_count': submitted,
                'withheld_count': withheld,
                'remaining_count': 0,
            })

        task = presentable[0]
        image_url, image_url_expires_at = _presign_task_image(job, task)
        payload: Dict[str, Any] = {
            'complete': False,
            'task_id': task['task_id'],
            'job_id': job_id,
            'image_url': image_url,
            'image_url_expires_at': image_url_expires_at,
            'task_type': job.get('task_type'),
            'label_set': job.get('label_set'),
            'submitted_count': submitted,
            'remaining_count': remaining,
            'withheld_count': withheld,
        }

        # Req 7.2: instructions and example images ride along when
        # stored; absent items are omitted from the payload.
        instructions = job.get('instructions')
        if instructions:
            payload['instructions'] = instructions
        example_urls = _example_image_urls(job)
        if example_urls:
            payload['example_images'] = example_urls

        # Req 8.3: the Pre_Label payload when generation succeeded.
        if task.get('prelabel_status') == 'Available':
            prelabel = _load_prelabel(task)
            if prelabel is not None:
                payload['prelabel'] = prelabel

        # Failure visibility (Req 7.5, 10.4): a Failed task is
        # presented as a bare image for annotation from scratch,
        # carrying its status and retained failure reason.
        if task.get('prelabel_status'):
            payload['prelabel_status'] = task['prelabel_status']
        if task.get('prelabel_error'):
            payload['prelabel_error'] = task['prelabel_error']

        return create_response(200, payload)

    except Exception as e:
        logger.error(f"Error fetching next labeler task: {str(e)}",
                     exc_info=True)
        return create_response(500, {'error': 'Failed to fetch next task'})


@rbac_check([Permission.LABELING_TASKS_SELF], allow_global=True)
def get_task_image_url(event, context):
    """GET /labeler/tasks/{taskId}/image-url[?job_id=]

    A fresh 15-minute presigned GET URL for the task's image after the
    previous grant expired; the client keeps its annotation state
    (Req 12.7).

    Task lookup approach (task 8.1): task ids are unique only within a
    job (table PK job_id, SK task_id) and this route carries no jobId,
    so the task is found through the caller's own assignee-index
    partition — which doubles as the ownership check: a task assigned
    to someone else is invisible here and denies exactly like a
    nonexistent one (Req 2.6). An optional job_id query parameter
    narrows the lookup; without it, an ambiguous task id across the
    caller's jobs prefers the unsubmitted (Assigned) match, then the
    lowest job_id, deterministically.
    """
    try:
        user = get_user_from_event(event)
        caller = user['user_id']
        task_id = (event.get('pathParameters') or {}).get('taskId')
        params = event.get('queryStringParameters') or {}
        query_job_id = params.get('job_id')

        candidates = [task
                      for task in _query_caller_tasks(caller, query_job_id)
                      if task.get('task_id') == task_id]
        if not candidates:
            return _labeler_access_denied(
                user, 'labeling_task', task_id,
                'task_not_assigned_to_caller')
        candidates.sort(key=lambda task: (
            0 if task.get('status') == 'Assigned' else 1,
            task.get('job_id', '')))
        task = candidates[0]

        job = labeling_jobs_table.get_item(
            Key={'job_id': task['job_id']}).get('Item')
        if not job or job.get('labeling_backend') != 'DDA':
            return _labeler_access_denied(
                user, 'labeling_task', task_id, 'job_not_accessible')
        if not _is_current_team_member(job.get('team_id'), caller):
            return _labeler_access_denied(
                user, 'labeling_task', task_id,
                'caller_not_current_team_member',
                job.get('usecase_id', ''))

        image_url, image_url_expires_at = _presign_task_image(job, task)
        return create_response(200, {
            'task_id': task_id,
            'job_id': task['job_id'],
            'image_url': image_url,
            'image_url_expires_at': image_url_expires_at,
        })

    except Exception as e:
        logger.error(f"Error refreshing task image URL: {str(e)}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Failed to refresh task image URL'})


# ---------------------------------------------------------------------------
# Labeler write routes (task 8.4 — Requirements 7.7, 7.8, 7.9, 7.12,
# 8.4, 11.6, 11.8)
# ---------------------------------------------------------------------------

def _resolve_caller_task_and_job(user: Dict, task_id: Optional[str],
                                 job_id: str) -> tuple:
    """Ownership resolution shared by the labeler POST routes
    (Req 2.4/2.6): the task must live in the caller's own
    assignee-index partition (assignee_user_id == caller sub) within a
    DDA job whose team the caller is currently a member of. Another
    labeler's task, a missing task, a missing/Ground Truth job, and a
    revoked membership are all indistinguishable to the caller: 403
    with no resource data plus a labeler_access_denied audit event.

    Returns (job, task, None) on success or (None, None, response).
    """
    caller = user['user_id']
    candidates = [task for task in _query_caller_tasks(caller, job_id)
                  if task.get('task_id') == task_id]
    if not candidates:
        return None, None, _labeler_access_denied(
            user, 'labeling_task', task_id, 'task_not_assigned_to_caller')
    task = candidates[0]

    job = labeling_jobs_table.get_item(Key={'job_id': job_id}).get('Item')
    if not job or job.get('labeling_backend') != 'DDA':
        return None, None, _labeler_access_denied(
            user, 'labeling_task', task_id, 'job_not_accessible')
    if not _is_current_team_member(job.get('team_id'), caller):
        return None, None, _labeler_access_denied(
            user, 'labeling_task', task_id,
            'caller_not_current_team_member', job.get('usecase_id', ''))
    return job, task, None


def _is_pixel_int(value: Any) -> bool:
    """A plain integer pixel measure (bool is an int subtype in Python
    and is not a coordinate)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_annotation(annotation: Any, job: Dict) -> List[Dict]:
    """Server-side completeness validation per modality (Req 7.8),
    mirroring the client-side blocking in the AnnotationCanvas. Each
    returned entry identifies the missing/invalid element.

    - Classification: annotation.label must be one of the job's
      Label_Set classes (no selection made -> rejected).
    - Segmentation: every region must carry a Label_Set class (class
      null — an unclassified SAM proposal — is rejected) and a
      non-empty RLE string.
    - ObjectDetection: every box must carry a Label_Set class and
      integer pixel coordinates lying within the image bounds declared
      by annotation.image_size.
    """
    modality = job.get('task_type')
    label_set = [str(name) for name in (job.get('label_set') or [])]

    if not isinstance(annotation, dict):
        return [_validation_error(
            'annotation', 'An annotation object is required')]

    errors: List[Dict] = []
    if annotation.get('modality') != modality:
        return [_validation_error(
            'modality',
            f"Annotation modality must be '{modality}' for this job",
            modality=annotation.get('modality'))]

    if modality == 'Classification':
        label = annotation.get('label')
        if label not in label_set:
            errors.append(_validation_error(
                'label',
                f"A classification selection from "
                f"{', '.join(label_set)} is required",
                label=label))

    elif modality == 'Segmentation':
        regions = annotation.get('regions')
        if not isinstance(regions, list):
            errors.append(_validation_error(
                'regions', 'regions must be a list of mask regions'))
            regions = []
        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                errors.append(_validation_error(
                    'regions', f'Region {index} must be an object'))
                continue
            region_class = region.get('class')
            if region_class not in label_set:
                errors.append(_validation_error(
                    'regions',
                    f'Region {index} lacks a class from the label set',
                    region_index=index, region_class=region_class))
            rle = region.get('rle')
            if not isinstance(rle, str) or not rle:
                errors.append(_validation_error(
                    'regions',
                    f'Region {index} lacks RLE mask data',
                    region_index=index))

    elif modality == 'ObjectDetection':
        image_size = annotation.get('image_size')
        image_size = image_size if isinstance(image_size, dict) else {}
        width = image_size.get('width')
        height = image_size.get('height')
        bounds_known = (_is_pixel_int(width) and width > 0
                        and _is_pixel_int(height) and height > 0)
        if not bounds_known:
            errors.append(_validation_error(
                'image_size',
                'image_size with positive integer width and height '
                'is required'))
        boxes = annotation.get('boxes')
        if not isinstance(boxes, list):
            errors.append(_validation_error(
                'boxes', 'boxes must be a list of bounding boxes'))
            boxes = []
        for index, box in enumerate(boxes):
            if not isinstance(box, dict):
                errors.append(_validation_error(
                    'boxes', f'Box {index} must be an object'))
                continue
            box_class = box.get('class')
            if box_class not in label_set:
                errors.append(_validation_error(
                    'boxes',
                    f'Box {index} lacks a class from the label set',
                    box_index=index, box_class=box_class))
            coords = [box.get(field)
                      for field in ('left', 'top', 'width', 'height')]
            if not all(_is_pixel_int(value) for value in coords):
                errors.append(_validation_error(
                    'boxes',
                    f'Box {index} must carry integer pixel left, top, '
                    f'width and height',
                    box_index=index))
                continue
            left, top, box_width, box_height = coords
            if (bounds_known
                    and not (left >= 0 and top >= 0
                             and box_width >= 1 and box_height >= 1
                             and left + box_width <= width
                             and top + box_height <= height)):
                errors.append(_validation_error(
                    'boxes',
                    f'Box {index} coordinates lie outside the '
                    f'{width}x{height} image bounds',
                    box_index=index))

    return errors


@rbac_check([Permission.LABELING_TASKS_SELF], allow_global=True)
def submit_labeler_task(event, context):
    """POST /labeler/tasks/{taskId}/submit  body: {job_id, annotation}

    Persist a complete annotation with the submitting labeler's
    identity and timestamp and mark the Task_Assignment Submitted
    (Req 7.7), recorded as human-annotated (Req 8.4). Submissions
    against a Stopped (or otherwise non-InProgress) job are rejected
    before anything is persisted (Req 11.8); incomplete annotations
    are rejected identifying the missing element with the task left
    unsubmitted (Req 7.8).

    Persistence is a conditional write (`status = Assigned AND
    assignee_user_id = :caller`) so double submits and stale
    assignments after rebalancing fail atomically, leaving the task in
    its prior state (Req 7.9). The annotation is stored inline for
    Classification/ObjectDetection; Segmentation region payloads (RLE
    JSON) are written to the portal artifacts bucket first, with
    annotation_s3_key on the item — an S3 failure leaves the task
    Assigned.

    The job's submitted_count is then incremented atomically (ADD with
    ReturnValues), making exactly one submitter observe
    submitted_count == image_count and async-invoke the worker with
    {action: 'generate_manifest', job_id} (Req 11.6).
    """
    try:
        user = get_user_from_event(event)
        caller = user['user_id']
        task_id = (event.get('pathParameters') or {}).get('taskId')

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {
                'error': 'Request body is not valid JSON'})
        job_id = body.get('job_id')
        if not job_id:
            return create_response(400, {'error': 'job_id is required'})

        job, task, denial = _resolve_caller_task_and_job(
            user, task_id, job_id)
        if denial:
            return denial

        # Req 11.8: a Stopped (or any non-InProgress) job rejects the
        # submission before anything is persisted.
        if job.get('status') != 'InProgress':
            return create_response(409, {
                'error': 'Labeling job is not in progress; the '
                         'submission was not saved',
                'job_id': job_id,
                'status': job.get('status'),
            })

        # Req 7.8: server-side completeness validation per modality;
        # rejections identify each missing element and persist nothing.
        annotation = body.get('annotation')
        validation_errors = _validate_annotation(annotation, job)
        if validation_errors:
            return create_response(400, {
                'error': 'Annotation is incomplete for the labeling '
                         'modality',
                'validation_errors': validation_errors,
                'task_id': task_id,
                'job_id': job_id,
            })

        now_epoch = int(datetime.utcnow().timestamp())
        now_iso = datetime.utcnow().isoformat() + 'Z'

        # Req 7.7/8.4: submitter identity, timestamps (epoch for
        # arithmetic, ISO-8601 for the manifest creation-date), and the
        # human-annotated marker.
        set_parts = [
            '#status = :submitted',
            'submitted_by = :submitted_by',
            'submitted_at = :submitted_at',
            'submitted_at_iso = :submitted_at_iso',
            'human_annotated = :human',
            'updated_at = :submitted_at',
        ]
        values: Dict[str, Any] = {
            ':submitted': 'Submitted',
            ':assigned': 'Assigned',
            ':caller': caller,
            ':submitted_by': caller,
            ':submitted_at': now_epoch,
            ':submitted_at_iso': now_iso,
            ':human': True,
        }

        if job.get('task_type') == 'Segmentation':
            # Segmentation region bitmaps (RLE JSON) go to S3; the item
            # carries annotation_s3_key. Written before the conditional
            # write so an S3 failure leaves the task Assigned (Req 7.9).
            if not PORTAL_ARTIFACTS_BUCKET:
                logger.error('PORTAL_ARTIFACTS_BUCKET is not configured; '
                             'segmentation annotation cannot be stored')
                return create_response(500, {
                    'error': 'Submission could not be saved; the task '
                             'remains unsubmitted'})
            annotation_key = (f"labeling/{job.get('usecase_id')}/{job_id}/"
                              f"annotations/{task_id}.json")
            try:
                s3_client.put_object(
                    Bucket=PORTAL_ARTIFACTS_BUCKET,
                    Key=annotation_key,
                    Body=json.dumps(annotation).encode(),
                    ContentType='application/json',
                )
            except Exception as e:  # noqa: BLE001 — Req 7.9
                logger.error(f"Segmentation annotation write failed for "
                             f"task {task_id} of job {job_id}: {e}")
                return create_response(500, {
                    'error': 'Submission could not be saved; the task '
                             'remains unsubmitted'})
            set_parts.append('annotation_s3_key = :annotation_s3_key')
            values[':annotation_s3_key'] = annotation_key
        else:
            set_parts.append('annotation = :annotation')
            values[':annotation'] = annotation

        try:
            labeling_tasks_table.update_item(
                Key={'job_id': job_id, 'task_id': task_id},
                UpdateExpression='SET ' + ', '.join(set_parts),
                ConditionExpression='#status = :assigned '
                                    'AND assignee_user_id = :caller',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues=values,
            )
        except ClientError as e:
            if (e.response.get('Error', {}).get('Code')
                    == 'ConditionalCheckFailedException'):
                # Double submit or a concurrent reassignment: the task
                # is no longer Assigned to the caller (Req 7.9).
                return create_response(409, {
                    'error': 'Task was already submitted or reassigned; '
                             'the submission was not saved',
                    'task_id': task_id,
                    'job_id': job_id,
                })
            logger.error(f"Annotation persistence failed for task "
                         f"{task_id} of job {job_id}: {e}")
            return create_response(500, {
                'error': 'Submission could not be saved; the task '
                         'remains unsubmitted'})

        # Req 11.6: atomic job-level counter; the returned value makes
        # exactly one submitter the manifest-generation trigger.
        job_submitted_count = None
        try:
            counter = labeling_jobs_table.update_item(
                Key={'job_id': job_id},
                UpdateExpression='SET updated_at = :now '
                                 'ADD submitted_count :one',
                ExpressionAttributeValues={':one': 1, ':now': now_epoch},
                ReturnValues='UPDATED_NEW',
            )
            job_submitted_count = int(
                counter.get('Attributes', {}).get('submitted_count', 0))
        except Exception as e:  # noqa: BLE001 — annotation is persisted
            logger.error(f"submitted_count increment failed for job "
                         f"{job_id}: {e}")

        image_count = int(job.get('image_count') or 0)
        if (job_submitted_count is not None and image_count > 0
                and job_submitted_count == image_count):
            _invoke_labeling_worker(
                {'action': 'generate_manifest', 'job_id': job_id})

        response: Dict[str, Any] = {
            'task_id': task_id,
            'job_id': job_id,
            'status': 'Submitted',
            'submitted_at': now_epoch,
            'submitted_at_iso': now_iso,
        }
        if job_submitted_count is not None:
            response['job_submitted_count'] = job_submitted_count
        return create_response(200, response)

    except Exception as e:
        logger.error(f"Error submitting labeler task: {str(e)}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Submission could not be saved; the task remains '
                     'unsubmitted'})


@rbac_check([Permission.LABELING_TASKS_SELF], allow_global=True)
def report_presentation_failure(event, context):
    """POST /labeler/tasks/{taskId}/presentation-failure
    body: {job_id, reason}

    Req 7.12: when a task's image cannot be retrieved or rendered, the
    presentation failure is recorded with the Task_Assignment
    (presentation_failure {reason, at}) and the task is withheld from
    labeling (status PresentationFailed — never served by the next-task
    gating). Ownership-checked like every labeler route; the write is
    conditional on the task still being Assigned to the caller, so a
    submitted task can never be withdrawn.
    """
    try:
        user = get_user_from_event(event)
        caller = user['user_id']
        task_id = (event.get('pathParameters') or {}).get('taskId')

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {
                'error': 'Request body is not valid JSON'})
        job_id = body.get('job_id')
        if not job_id:
            return create_response(400, {'error': 'job_id is required'})
        reason = body.get('reason')
        reason = (reason.strip() if isinstance(reason, str)
                  and reason.strip() else 'unspecified')

        job, task, denial = _resolve_caller_task_and_job(
            user, task_id, job_id)
        if denial:
            return denial

        now_epoch = int(datetime.utcnow().timestamp())
        try:
            labeling_tasks_table.update_item(
                Key={'job_id': job_id, 'task_id': task_id},
                UpdateExpression='SET #status = :failed, '
                                 'presentation_failure = :failure, '
                                 'updated_at = :now',
                ConditionExpression='#status = :assigned '
                                    'AND assignee_user_id = :caller',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':failed': 'PresentationFailed',
                    ':failure': {'reason': reason, 'at': now_epoch},
                    ':assigned': 'Assigned',
                    ':caller': caller,
                    ':now': now_epoch,
                },
            )
        except ClientError as e:
            if (e.response.get('Error', {}).get('Code')
                    == 'ConditionalCheckFailedException'):
                return create_response(409, {
                    'error': 'Task is no longer assigned to you; the '
                             'presentation failure was not recorded',
                    'task_id': task_id,
                    'job_id': job_id,
                })
            raise

        return create_response(200, {
            'task_id': task_id,
            'job_id': job_id,
            'status': 'PresentationFailed',
            'presentation_failure': {'reason': reason, 'at': now_epoch},
        })

    except Exception as e:
        logger.error(f"Error recording presentation failure: {str(e)}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Failed to record the presentation failure'})


# ---------------------------------------------------------------------------
# Skip-verification Admin_Review (task 11.3 — Requirements 9.5, 9.6,
# 9.7, 9.8, 9.9, 9.10, 11.6)
# ---------------------------------------------------------------------------
# Authorization model: @rbac_check([MANAGE_LABELING_JOBS]) gates the
# routes (the job's Use_Case scope is injected by the router), then an
# explicit UseCaseAdmin/PortalAdmin role check — the same check
# skip-verification job creation applies (Req 9.1) — rejects
# non-admin holders of the permission with 403 plus an
# unauthorized_access audit event. Only skip-verification DDA jobs
# have an Admin_Review; any other job answers 400.


def _require_review_admin(user: Dict, job: Dict) -> Optional[Dict]:
    """UseCaseAdmin/PortalAdmin only, consistent with skip-verification
    job creation (Req 9.1). Returns None when authorized or the 403
    response otherwise."""
    role = rbac_manager.get_user_role(
        user['user_id'], job.get('usecase_id') or 'global', user)
    role_value = role.value if role else None
    if role_value in SKIP_VERIFICATION_ADMIN_ROLES:
        return None
    log_audit_event(
        user_id=user['user_id'],
        action='unauthorized_access',
        resource_type='labeling_job',
        resource_id=job.get('job_id', 'unknown'),
        result='denied',
        details={
            'usecase_id': job.get('usecase_id', ''),
            'reason': 'admin review requires administrator authorization',
        },
    )
    return create_response(403, {
        'error': 'Admin review requires administrator authorization',
    })


def _load_review_job(job_id: Optional[str]) -> tuple:
    """(job, None) for a reviewable job or (None, error response):
    404 for a missing job, 400 for anything that is not a
    skip-verification DDA job (only those have an Admin_Review)."""
    job = (labeling_jobs_table.get_item(Key={'job_id': job_id})
           .get('Item') if job_id else None)
    if not job:
        return None, create_response(404, {
            'error': 'Labeling job not found'})
    if (job.get('labeling_backend') != 'DDA'
            or not job.get('skip_verification')):
        return None, create_response(400, {
            'error': 'Admin review is only available for '
                     'skip-verification DDA labeling jobs',
            'job_id': job_id,
            'labeling_backend': job.get('labeling_backend'),
            'skip_verification': bool(job.get('skip_verification')),
        })
    return job, None


def _encode_review_token(last_evaluated_key: Dict) -> str:
    """Opaque pagination token: base64 of the DynamoDB
    ExclusiveStartKey."""
    return base64.urlsafe_b64encode(
        json.dumps(last_evaluated_key).encode()).decode()


def _decode_review_token(next_token: str) -> Optional[Dict]:
    """The ExclusiveStartKey encoded by _encode_review_token, or None
    when the token is malformed."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(
            next_token.encode()).decode())
        return decoded if isinstance(decoded, dict) else None
    except Exception:  # noqa: BLE001 — any malformed token is invalid
        return None


def _query_all_job_tasks(job_id: str) -> List[Dict]:
    """Every result item of the job (full paginated query) — the
    finalize gating must see the whole dataset (Req 9.7, 9.8)."""
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


def _review_item_payload(job: Dict, task: Dict) -> Dict:
    """One Admin_Review listing entry (Req 9.5, 9.10): task_id,
    image_key, succeeded/failed status (succeeded exactly when the
    auto-label result is Available), the pre-label annotation inline,
    autolabel_error for failed items, the current decision, and a
    presigned image URL."""
    succeeded = task.get('prelabel_status') == 'Available'
    item: Dict[str, Any] = {
        'task_id': task.get('task_id'),
        'image_key': task.get('image_key'),
        'status': 'succeeded' if succeeded else 'failed',
    }
    # Failure visibility (Req 7.6, 7.7, 10.4): the raw per-task
    # generation status and retained failure reason ride along so a
    # failed image displays why; decision gating is unchanged.
    if task.get('prelabel_status'):
        item['prelabel_status'] = task['prelabel_status']
    if task.get('prelabel_error'):
        item['prelabel_error'] = task['prelabel_error']
    if succeeded:
        prelabel = _load_prelabel(task)
        if prelabel is not None:
            item['prelabel'] = prelabel
    else:
        item['autolabel_error'] = task.get('autolabel_error',
                                           'auto-labeling did not complete')
    if task.get('review_decision'):
        item['review_decision'] = task['review_decision']
    try:
        image_url, expires_at = _presign_task_image(job, task)
        item['image_url'] = image_url
        item['image_url_expires_at'] = expires_at
    except Exception as e:  # noqa: BLE001 — the entry still lists
        logger.warning(f"Could not presign review image for task "
                       f"{task.get('task_id')} of job "
                       f"{job.get('job_id')}: {e}")
    return item


@rbac_check([Permission.MANAGE_LABELING_JOBS], allow_global=True)
def get_admin_review(event, context):
    """GET /labeling/{id}/review?limit=&next_token=

    Paginated Admin_Review listing covering every dataset image of a
    skip-verification job (Req 9.5): the tasks table queried with
    Limit + ExclusiveStartKey, the cursor carried as an opaque base64
    next_token. Each entry names the image's succeeded/failed status,
    the pre-label annotation, the failure reason for failed items
    (ineligible for acceptance, Req 9.10), the current decision, and a
    presigned image URL.
    """
    try:
        user = get_user_from_event(event)
        job_id = (event.get('pathParameters') or {}).get('id')

        job, error_response = _load_review_job(job_id)
        if error_response:
            return error_response
        denial = _require_review_admin(user, job)
        if denial:
            return denial

        params = event.get('queryStringParameters') or {}
        try:
            limit = int(params.get('limit') or REVIEW_PAGE_SIZE_DEFAULT)
        except (TypeError, ValueError):
            return create_response(400, {
                'error': 'limit must be a positive integer'})
        if limit < 1:
            return create_response(400, {
                'error': 'limit must be a positive integer'})
        limit = min(limit, REVIEW_PAGE_SIZE_MAX)

        kwargs: Dict[str, Any] = {
            'KeyConditionExpression': 'job_id = :jid',
            'ExpressionAttributeValues': {':jid': job_id},
            'Limit': limit,
        }
        next_token = params.get('next_token')
        if next_token:
            start_key = _decode_review_token(next_token)
            if not start_key:
                return create_response(400, {
                    'error': 'next_token is not a valid pagination token'})
            kwargs['ExclusiveStartKey'] = start_key

        response = labeling_tasks_table.query(**kwargs)
        items = [_review_item_payload(job, task)
                 for task in response.get('Items', [])]

        payload: Dict[str, Any] = {
            'job_id': job_id,
            'items': items,
            'count': len(items),
            'review_finalized': bool(job.get('review_finalized')),
        }
        last_key = response.get('LastEvaluatedKey')
        if last_key:
            payload['next_token'] = _encode_review_token(last_key)
        return create_response(200, payload)

    except Exception as e:
        logger.error(f"Error listing admin review: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to list the review'})


@rbac_check([Permission.MANAGE_LABELING_JOBS], allow_global=True)
def save_review_decisions(event, context):
    """POST /labeling/{id}/review/decisions
    body: {decisions: {task_id: 'accepted'|'rejected'}}

    Batch per-image decision upserts (Req 9.6): every decision is
    validated before anything is persisted — unknown task ids and
    non-accepted/rejected values are 400s, and failed items
    (prelabel_status != Available) are ineligible for 'accepted'
    (Req 9.10), rejected with a 400 identifying them. Decisions stay
    mutable until the review is finalized; afterwards every change is
    rejected with 409 and nothing is written.
    """
    try:
        user = get_user_from_event(event)
        job_id = (event.get('pathParameters') or {}).get('id')

        job, error_response = _load_review_job(job_id)
        if error_response:
            return error_response
        denial = _require_review_admin(user, job)
        if denial:
            return denial

        # Req 9.6: decisions are immutable after finalization.
        if job.get('review_finalized'):
            return create_response(409, {
                'error': 'The admin review has been finalized; decisions '
                         'can no longer be changed',
                'job_id': job_id,
            })

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {
                'error': 'Request body is not valid JSON'})
        decisions = body.get('decisions')
        if not isinstance(decisions, dict) or not decisions:
            return create_response(400, {
                'error': "decisions must be a non-empty object of the "
                         "form {task_id: 'accepted'|'rejected'}"})

        invalid_values = {
            task_id: decision for task_id, decision in decisions.items()
            if decision not in REVIEW_DECISIONS}
        if invalid_values:
            return create_response(400, {
                'error': "Each decision must be 'accepted' or 'rejected'",
                'invalid_decisions': invalid_values,
            })

        # Validate every referenced task before persisting anything.
        tasks: Dict[str, Dict] = {}
        unknown_ids: List[str] = []
        for task_id in decisions:
            task = labeling_tasks_table.get_item(
                Key={'job_id': job_id, 'task_id': task_id}).get('Item')
            if task:
                tasks[task_id] = task
            else:
                unknown_ids.append(task_id)
        if unknown_ids:
            return create_response(400, {
                'error': 'Decisions reference task ids that do not exist '
                         'in this job',
                'unknown_task_ids': sorted(unknown_ids),
            })

        # Req 9.10: failed results are ineligible for acceptance.
        ineligible = sorted(
            task_id for task_id, decision in decisions.items()
            if decision == 'accepted'
            and tasks[task_id].get('prelabel_status') != 'Available')
        if ineligible:
            return create_response(400, {
                'error': 'Failed auto-label results are ineligible for '
                         'acceptance',
                'ineligible_task_ids': ineligible,
            })

        now = int(datetime.utcnow().timestamp())
        for task_id, decision in decisions.items():
            labeling_tasks_table.update_item(
                Key={'job_id': job_id, 'task_id': task_id},
                UpdateExpression='SET review_decision = :decision, '
                                 'updated_at = :now',
                ExpressionAttributeValues={
                    ':decision': decision,
                    ':now': now,
                },
            )

        return create_response(200, {
            'job_id': job_id,
            'updated_count': len(decisions),
            'message': 'Review decisions saved',
        })

    except Exception as e:
        logger.error(f"Error saving review decisions: {str(e)}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Failed to save review decisions'})


@rbac_check([Permission.MANAGE_LABELING_JOBS], allow_global=True)
def finalize_admin_review(event, context):
    """POST /labeling/{id}/review/finalize

    Finalize gating (Req 9.7, 9.8): every successfully auto-labeled
    (Available) result must carry a decision — otherwise 400 with the
    undecided count and the review stays open with all decisions
    retained — and at least one result must be accepted. On success
    review_finalized=true is set with a conditional write (a review
    already finalized answers 409) and the worker is async-invoked
    with {action: 'generate_manifest', job_id} to emit exactly the
    accepted set (Req 9.9, 11.6).
    """
    try:
        user = get_user_from_event(event)
        job_id = (event.get('pathParameters') or {}).get('id')

        job, error_response = _load_review_job(job_id)
        if error_response:
            return error_response
        denial = _require_review_admin(user, job)
        if denial:
            return denial

        if job.get('review_finalized'):
            return create_response(409, {
                'error': 'The admin review has already been finalized',
                'job_id': job_id,
            })

        tasks = _query_all_job_tasks(job_id)

        # Req 9.7: every Available result needs a decision.
        undecided_count = sum(
            1 for task in tasks
            if task.get('prelabel_status') == 'Available'
            and task.get('review_decision') not in REVIEW_DECISIONS)
        if undecided_count:
            return create_response(400, {
                'error': f'{undecided_count} auto-labeled image(s) have '
                         f'neither an accept nor a reject decision; the '
                         f'review remains open',
                'undecided_count': undecided_count,
                'job_id': job_id,
            })

        # Req 9.8: at least one accepted result is required.
        accepted_count = sum(
            1 for task in tasks
            if task.get('prelabel_status') == 'Available'
            and task.get('review_decision') == 'accepted')
        if accepted_count == 0:
            return create_response(400, {
                'error': 'At least one accepted result is required to '
                         'finalize the review',
                'accepted_count': 0,
                'job_id': job_id,
            })

        now = int(datetime.utcnow().timestamp())
        try:
            # Conditional on not already finalized, so concurrent
            # finalizations trigger exactly one manifest generation.
            labeling_jobs_table.update_item(
                Key={'job_id': job_id},
                UpdateExpression='SET review_finalized = :true, '
                                 'review_finalized_at = :now, '
                                 'updated_at = :now',
                ConditionExpression='attribute_not_exists(review_finalized) '
                                    'OR review_finalized = :false',
                ExpressionAttributeValues={
                    ':true': True,
                    ':false': False,
                    ':now': now,
                },
            )
        except ClientError as e:
            if (e.response.get('Error', {}).get('Code')
                    == 'ConditionalCheckFailedException'):
                return create_response(409, {
                    'error': 'The admin review has already been finalized',
                    'job_id': job_id,
                })
            raise

        log_audit_event(
            user_id=user['user_id'],
            action='review_finalized',
            resource_type='labeling_job',
            resource_id=job_id,
            result='success',
            details={
                'usecase_id': job.get('usecase_id', ''),
                'accepted_count': accepted_count,
            },
        )

        # Req 9.9/11.6: the manifest generator emits exactly the
        # accepted set.
        _invoke_labeling_worker(
            {'action': 'generate_manifest', 'job_id': job_id})

        return create_response(200, {
            'job_id': job_id,
            'review_finalized': True,
            'accepted_count': accepted_count,
            'message': 'Review finalized; manifest generation started',
        })

    except Exception as e:
        logger.error(f"Error finalizing admin review: {str(e)}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Failed to finalize the admin review'})


# ---------------------------------------------------------------------------
# Preview_Run state helpers (task 8.1 — llm-autolabel-prompt-tuning
# Requirements 1.6, 3.5, 8.7, 8.8)
# ---------------------------------------------------------------------------
# A Prompt_Tuning_Preview run keeps all of its state in resources that
# already exist, so no new table and no new GSI are introduced:
#
#   dda-portal-labeling-tasks   PREVIEW#{run_id}      / RUN
#                               PREVIEW#{run_id}      / IMAGE#{i:03d}
#                               PREVIEWLOCK#{usecase} / USER#{user_sub}
#   portal artifacts bucket     labeling-previews/{usecase_id}/{run_id}/{i}.json
#
# IMPORTANT: preview items deliberately carry **no `assignee_user_id`
# attribute**. The tasks table's `assignee-index` GSI is keyed on
# assignee_user_id, and DynamoDB only projects an item into a GSI when
# the item has the index's key attributes — so preview items are
# invisible to `_query_caller_tasks` and therefore to every labeler API,
# by construction rather than by a filter that could be forgotten
# (Req 1.6: a Preview_Run creates no Task_Assignment a labeler can see).
#
# Item expiry is enforced by comparing `expires_at` explicitly, both in
# the lock's conditional write and in every read. The `ttl` attribute
# (the tasks table's TTL attribute, enabled in storage-stack.ts) is
# cleanup only — correctness never depends on DynamoDB reaping an item.

PREVIEW_RUN_PK_PREFIX = 'PREVIEW#'
PREVIEW_RUN_SK = 'RUN'
PREVIEW_SAMPLE_SK_PREFIX = 'IMAGE#'
PREVIEW_LOCK_PK_PREFIX = 'PREVIEWLOCK#'
PREVIEW_LOCK_SK_PREFIX = 'USER#'

# Run statuses on the RUN item.
PREVIEW_STATUS_RUNNING = 'Running'
PREVIEW_STATUS_COMPLETED = 'Completed'
PREVIEW_STATUS_FAILED = 'Failed'
# Per-sample states on the IMAGE#{i} items.
PREVIEW_SAMPLE_PENDING = 'Pending'
PREVIEW_SAMPLE_SUCCEEDED = 'Succeeded'
PREVIEW_SAMPLE_FAILED = 'Failed'

# Sample_Image count bounds for one Preview_Run (Req 8.4).
PREVIEW_SAMPLE_MIN = 1
PREVIEW_SAMPLE_MAX = 5
# Per-Sample_Image model invocation bound, matching the Auto_Labeler's
# (Req 3.3) — also the unit the lock TTL is derived from.
PREVIEW_PER_SAMPLE_SECONDS = 120
# Lock slack (start-up, S3 reads) and the Lambda timeout ceiling.
PREVIEW_LOCK_SLACK_SECONDS = 60
PREVIEW_LOCK_TTL_MAX_SECONDS = 900
# Grace period between the logical `expires_at` and the DynamoDB `ttl`
# reap time, so an item stays readable for diagnosis after it expires.
PREVIEW_ITEM_TTL_GRACE_SECONDS = 3600
# Preview result payloads: ephemeral, artifacts-bucket-only, expired by
# a bucket lifecycle rule after one day (Req 1.6, 3.5).
PREVIEW_RESULT_PREFIX = 'labeling-previews/'
PREVIEW_RESULT_URL_EXPIRY_SECONDS = 900
# Exactly one of these categories is attributed to a failed
# Preview_Result (Req 9.6). The first three come from
# dda_llm_prelabel.LlmPrelabelError (shared with the Auto_Labeler); the
# last three are raised by the executor before any invocation.
PREVIEW_FAILURE_CATEGORIES = (
    'model_error',
    'timeout',
    'unusable_model_output',
    'image_access_failure',
    'unsupported_image_content',
    'unreadable_example_image',
)


def _now_epoch() -> int:
    """Current time as whole epoch seconds — the unit every preview
    `expires_at` / `ttl` comparison uses."""
    return int(datetime.utcnow().timestamp())


def _new_preview_run_id() -> str:
    """A fresh Preview_Run identifier (`preview-<8 hex>`), following the
    portal's `labeling-<8 hex>` job id convention."""
    return f"preview-{uuid.uuid4().hex[:8]}"


def _preview_run_pk(run_id: str) -> str:
    """Partition key shared by a run's RUN and IMAGE#{i} items."""
    return f'{PREVIEW_RUN_PK_PREFIX}{run_id}'


def _preview_sample_sk(index: int) -> str:
    """Sort key of the sample at `index`. Zero-padded to three digits so
    a Query returns samples in request order without a client-side
    sort (Req 3.5, 4.6: one entry per Sample_Image, in request order)."""
    return f'{PREVIEW_SAMPLE_SK_PREFIX}{index:03d}'


def _preview_sample_index(task_id: str) -> int:
    """The integer index encoded in an `IMAGE#{i:03d}` sort key."""
    return int(task_id[len(PREVIEW_SAMPLE_SK_PREFIX):])


def _preview_lock_ttl_seconds(sample_count: int) -> int:
    """`min(sample_count * 120 + 60, 900)` (Req 8.8).

    The claim can never outlive the executor: the run itself is bounded
    by `sample_count` invocations of at most 120 s each, and the
    executor's own Lambda timeout is 900 s. So a crashed or timed-out
    executor self-heals within one run bound and the next request from
    the same user succeeds — no reaper process is needed and no lock can
    wedge a Use_Case permanently.
    """
    return min(
        max(int(sample_count), 0) * PREVIEW_PER_SAMPLE_SECONDS
        + PREVIEW_LOCK_SLACK_SECONDS,
        PREVIEW_LOCK_TTL_MAX_SECONDS,
    )


def _claim_preview_lock(usecase_id: str, user_sub: str, run_id: str,
                        sample_count: int) -> bool:
    """Claim the per-user, per-Use_Case in-flight lock (Req 8.8).

    Returns True when the claim succeeded and False when this user
    already has a Preview_Run in flight in this Use_Case. The whole
    guard is one conditional write, so two concurrent requests can never
    both win:

        attribute_not_exists(task_id) OR expires_at < :now

    `expires_at` is compared explicitly rather than relying on the
    table's TTL, because TTL deletion is asynchronous and best-effort —
    an expired-but-not-yet-reaped lock must not block a new run.

    The caller answers 409 on False and, per Req 8.8, reads no object
    and invokes no model on that path.
    """
    now = _now_epoch()
    expires_at = now + _preview_lock_ttl_seconds(sample_count)
    try:
        labeling_tasks_table.put_item(
            Item={
                'job_id': f'{PREVIEW_LOCK_PK_PREFIX}{usecase_id}',
                'task_id': f'{PREVIEW_LOCK_SK_PREFIX}{user_sub}',
                'run_id': run_id,
                'claimed_at': now,
                'expires_at': expires_at,
                'ttl': expires_at + PREVIEW_ITEM_TTL_GRACE_SECONDS,
            },
            ConditionExpression=('attribute_not_exists(task_id) '
                                 'OR expires_at < :now'),
            ExpressionAttributeValues={':now': now},
        )
        return True
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            return False
        raise


def _release_preview_lock(usecase_id: str, user_sub: str) -> None:
    """Release the in-flight lock unconditionally (Req 8.8).

    Unconditional on purpose: the executor calls this on every terminal
    path, including unexpected exceptions, and a release must never fail
    because the lock was already reaped by TTL or overwritten by a
    later claim. Deleting a key that does not exist is a no-op in
    DynamoDB.
    """
    labeling_tasks_table.delete_item(
        Key={
            'job_id': f'{PREVIEW_LOCK_PK_PREFIX}{usecase_id}',
            'task_id': f'{PREVIEW_LOCK_SK_PREFIX}{user_sub}',
        },
    )


def _read_preview_lock(usecase_id: str, user_sub: str) -> Optional[Dict]:
    """The user's *active* lock item for this Use_Case, or None.

    A lock whose `expires_at` has passed reads as absent — the same
    explicit comparison the conditional write makes, so reads and writes
    can never disagree about whether a run is still in flight.
    """
    item = labeling_tasks_table.get_item(
        Key={
            'job_id': f'{PREVIEW_LOCK_PK_PREFIX}{usecase_id}',
            'task_id': f'{PREVIEW_LOCK_SK_PREFIX}{user_sub}',
        },
    ).get('Item')
    if not item:
        return None
    if int(item.get('expires_at', 0)) < _now_epoch():
        return None
    return item


def _preview_stored_examples(examples: List[Any]) -> List[Dict]:
    """The Few_Shot_Example references to record on the RUN item, in
    stored order (task 9.1).

    Normalized to the persisted job-record shape — `{ref, designation,
    position}` — so the executor feeds `select_few_shot_examples` exactly
    what the Auto_Labeler feeds it from `auto_label.few_shot.examples`
    (Req 6.6, 7.6). Order is preserved verbatim: it is the ordering the
    selection depends on, and it is the ordering the start route already
    used to compute the attached/omitted counts.

    Entries that could not be attached at all (no usable reference) are
    dropped, which by the time this runs is unreachable — the request
    validation rejects them — but keeps the recorded set and the counts
    describing the same references.
    """
    stored: List[Dict] = []
    for index, example in enumerate(examples or []):
        if not isinstance(example, dict):
            continue
        ref = example.get('ref')
        if not isinstance(ref, str) or not ref.strip():
            continue
        position = example.get('position')
        stored.append({
            'ref': ref.strip(),
            'designation': (example.get('designation')
                            if example.get('designation')
                            in (FEW_SHOT_GOOD, FEW_SHOT_BAD)
                            else FEW_SHOT_BAD),
            'position': (int(position)
                         if isinstance(position, int)
                         and not isinstance(position, bool)
                         else index),
        })
    return stored


def _write_preview_run_item(run_id: str, usecase_id: str, created_by: str,
                            model: str, task_type: str,
                            label_set: List[str], detection_prompt: str,
                            sample_count: int, few_shot_enabled: bool,
                            attached_example_count: int,
                            omitted_example_count: int = 0,
                            few_shot_examples: Optional[List[Dict]] = None,
                            downscale_max_edge: Optional[int] = None,
                            token_budget: Optional[int] = None
                            ) -> Dict:
    """Write the `PREVIEW#{run_id}` / `RUN` item in status Running.

    Carries everything the status route needs to answer without
    re-reading the request, and no `assignee_user_id` — see the module
    note above on why that keeps the item out of `assignee-index`
    (Req 1.6).

    `attached_example_count` / `omitted_example_count` are both recorded
    (task 8.3) because they are the two halves of one
    `select_few_shot_examples` result computed at start time: storing the
    omitted count rather than re-deriving it from a total keeps the
    status route's attached/omitted report consistent with what the
    executor actually attaches, by construction (Req 7.2, 7.5, 7.6).

    `few_shot_examples` (task 9.1) carries the *validated* example
    references in stored order, so the executor attaches the same set the
    request asked for without the request body being kept anywhere else —
    the same relationship the Auto_Labeler has with
    `auto_label.few_shot.examples` on the job record (Req 6.6, 7.6). The
    key is written only when there is something to attach, so a run
    without Few_Shot_Examples produces exactly the item shape task 8.2
    wrote.

    `downscale_max_edge` / `token_budget`
    (llm-model-token-and-image-sizing Req 5.3, 1.6, 9.5):
    `downscale_max_edge` is the validated Max_Image_Edge integer,
    recorded only when a bound was selected — Downscale_Off leaves the
    attribute absent, exactly as a pre-feature run item reads.
    `token_budget` is the Effective_Token_Budget already resolved by the
    start route, not the raw selection: the executor passes it back in
    as the selection (re-resolution of a resolved value is the
    identity), so the budget recorded here, the budget audited, the
    budget the status route reports and the budget actually sent are the
    same integer by construction.
    """
    now = _now_epoch()
    expires_at = now + _preview_lock_ttl_seconds(sample_count)
    item = {
        'job_id': _preview_run_pk(run_id),
        'task_id': PREVIEW_RUN_SK,
        'usecase_id': usecase_id,
        'created_by': created_by,
        'model': model,
        'task_type': task_type,
        'label_set': label_set,
        'detection_prompt': detection_prompt,
        'few_shot_enabled': bool(few_shot_enabled),
        'attached_example_count': int(attached_example_count),
        'omitted_example_count': int(omitted_example_count),
        'sample_count': int(sample_count),
        'status': PREVIEW_STATUS_RUNNING,
        'created_at': now,
        'expires_at': expires_at,
        'ttl': expires_at + PREVIEW_ITEM_TTL_GRACE_SECONDS,
    }
    if few_shot_examples:
        item['few_shot_examples'] = few_shot_examples
    if downscale_max_edge is not None:
        item['downscale_max_edge'] = int(downscale_max_edge)
    if token_budget is not None:
        item['token_budget'] = int(token_budget)
    labeling_tasks_table.put_item(Item=item)
    return item


def _write_preview_sample_items(run_id: str,
                                sample_keys: List[str]) -> None:
    """Write one `IMAGE#{i:03d}` item per requested Sample_Image in
    state Pending (Req 3.5, 4.6).

    Every requested sample gets its item up front, so the status route
    returns exactly one entry per Sample_Image for the whole life of the
    run — including before the executor has reached it.
    """
    now = _now_epoch()
    expires_at = now + _preview_lock_ttl_seconds(len(sample_keys))
    with labeling_tasks_table.batch_writer() as batch:
        for index, sample_key in enumerate(sample_keys):
            batch.put_item(Item={
                'job_id': _preview_run_pk(run_id),
                'task_id': _preview_sample_sk(index),
                'sample_key': sample_key,
                'state': PREVIEW_SAMPLE_PENDING,
                'created_at': now,
                'ttl': expires_at + PREVIEW_ITEM_TTL_GRACE_SECONDS,
            })


def _read_preview_run_item(run_id: str) -> Optional[Dict]:
    """The run's `RUN` item, or None for an unknown run id."""
    return labeling_tasks_table.get_item(
        Key={'job_id': _preview_run_pk(run_id),
             'task_id': PREVIEW_RUN_SK},
    ).get('Item')


def _read_preview_sample_items(run_id: str) -> List[Dict]:
    """The run's sample items ordered by request index.

    The `IMAGE#` sort-key prefix excludes the `RUN` item, and the
    zero-padded index makes DynamoDB's lexicographic sort order the
    request order (Req 3.5, 4.6).
    """
    items: List[Dict] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression':
            'job_id = :pk AND begins_with(task_id, :sk)',
        'ExpressionAttributeValues': {
            ':pk': _preview_run_pk(run_id),
            ':sk': PREVIEW_SAMPLE_SK_PREFIX,
        },
    }
    while True:
        response = labeling_tasks_table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return sorted(items, key=lambda item: item['task_id'])


def _update_preview_sample_state(
        run_id: str, index: int, state: str,
        failure_category: Optional[str] = None,
        failure_reason: Optional[str] = None,
        result_s3_key: Optional[str] = None) -> None:
    """Resolve one sample to Succeeded or Failed.

    Written immediately as each sample resolves, so the wizard's polling
    renders progressively and a per-sample failure is recorded without
    disturbing any other sample (Req 3.7).
    """
    updates = ['#state = :state', 'resolved_at = :now']
    values: Dict[str, Any] = {':state': state, ':now': _now_epoch()}
    if failure_category is not None:
        updates.append('failure_category = :category')
        values[':category'] = failure_category
    if failure_reason is not None:
        updates.append('failure_reason = :reason')
        values[':reason'] = failure_reason
    if result_s3_key is not None:
        updates.append('result_s3_key = :result_key')
        values[':result_key'] = result_s3_key
    labeling_tasks_table.update_item(
        Key={'job_id': _preview_run_pk(run_id),
             'task_id': _preview_sample_sk(index)},
        UpdateExpression='SET ' + ', '.join(updates),
        # `state` is a DynamoDB reserved word.
        ExpressionAttributeNames={'#state': 'state'},
        ExpressionAttributeValues=values,
    )


def _update_preview_run_status(run_id: str, status: str,
                               run_error: Optional[str] = None) -> None:
    """Move the run to a terminal status (Completed or Failed).

    A run whose every sample failed still reaches Completed — the run
    itself succeeded in producing an outcome per sample. Failed is for
    the run as a whole (e.g. the async invoke never landed).
    """
    updates = ['#status = :status', 'updated_at = :now']
    values: Dict[str, Any] = {':status': status, ':now': _now_epoch()}
    if run_error is not None:
        updates.append('run_error = :run_error')
        values[':run_error'] = run_error
    labeling_tasks_table.update_item(
        Key={'job_id': _preview_run_pk(run_id),
             'task_id': PREVIEW_RUN_SK},
        UpdateExpression='SET ' + ', '.join(updates),
        # `status` is a DynamoDB reserved word.
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues=values,
    )


def _preview_result_key(usecase_id: str, run_id: str, index: int) -> str:
    """`labeling-previews/{usecase_id}/{run_id}/{i}.json`.

    Deliberately **not** under `labeling/{usecase_id}/{job_id}/prelabels/`:
    a Preview_Run must produce no pipeline Pre_Label artifact (Req 1.6,
    3.5). This prefix is referenced by no Labeling_Job and no
    Task_Assignment, is readable only through the run's own presigned
    URL, and is expired by the artifacts bucket lifecycle rule after one
    day.
    """
    return f'{PREVIEW_RESULT_PREFIX}{usecase_id}/{run_id}/{index}.json'


def _preview_success_payload(sample_key: str, prelabel: Dict,
                             width: int, height: int,
                             sent_dimensions: Optional[tuple] = None,
                             downscale_max_edge: Optional[int] = None
                             ) -> Dict:
    """Result payload for a Sample_Image that produced a Pre_Label.

    `image_width` / `image_height` keep their pre-feature meaning — the
    Source_Dimensions, the coordinate space the Pre_Label geometry is
    expressed in — so `PreviewResultCanvas` reads them unchanged
    (llm-model-token-and-image-sizing Req 7.7).

    Where the run carries a Downscale_Setting (`downscale_max_edge` is a
    Max_Image_Edge value), the payload additionally carries the sizing
    report (Req 5.4, 5.10): the Source_Dimensions duplicated into the
    explicit `source_width` / `source_height`, the Sent_Dimensions of
    the image actually sent as `sent_width` / `sent_height`, and the
    applied `downscale_max_edge`. A Downscale_Off run keeps the payload
    byte-identical to its pre-feature shape — sent equals source is
    exactly the pre-feature meaning, and the wizard renders its
    unavailable branch for the sizing row (Req 5.11) — so an
    unconfigured run stays purely pre-feature end to end (Req 10.1).
    """
    payload = {
        'sample_key': sample_key,
        'state': PREVIEW_SAMPLE_SUCCEEDED,
        'prelabel': prelabel,
        'image_width': int(width),
        'image_height': int(height),
    }
    if downscale_max_edge is not None:
        sent_width, sent_height = sent_dimensions or (width, height)
        payload['source_width'] = int(width)
        payload['source_height'] = int(height)
        payload['sent_width'] = int(sent_width)
        payload['sent_height'] = int(sent_height)
        payload['downscale_max_edge'] = int(downscale_max_edge)
    return payload


def _preview_failure_payload(sample_key: str, failure_category: str,
                             failure_reason: str,
                             raw_model_output: Optional[str] = None) -> Dict:
    """Result payload for a failed Sample_Image (Req 9.1-9.6).

    `raw_model_output` is included verbatim, with no truncation and no
    normalization, only when a model response was actually received —
    that is what lets the wizard show the complete raw text for an
    `unusable_model_output` result (Req 9.3, 9.8).
    """
    payload = {
        'sample_key': sample_key,
        'state': PREVIEW_SAMPLE_FAILED,
        'failure_category': failure_category,
        'failure_reason': failure_reason,
    }
    if raw_model_output is not None:
        payload['raw_model_output'] = raw_model_output
    return payload


def _write_preview_result_payload(usecase_id: str, run_id: str, index: int,
                                  payload: Dict) -> str:
    """Persist one result payload to the portal artifacts bucket and
    return its key.

    Payloads live in S3 rather than on the DynamoDB item because a
    Segmentation Pre_Label carries an RLE counts string per region and
    raw model output must be kept character-for-character, either of
    which can exceed the 400 KB item limit.
    """
    key = _preview_result_key(usecase_id, run_id, index)
    s3_client.put_object(
        Bucket=PORTAL_ARTIFACTS_BUCKET,
        Key=key,
        Body=json.dumps(payload).encode('utf-8'),
        ContentType='application/json',
    )
    return key


def _presign_preview_result(result_s3_key: Optional[str]) -> Optional[str]:
    """A read-only presigned GET URL for one result payload, or None.

    Scoped to exactly one object and valid for 15 minutes, matching the
    labeler image-grant bound. Returns None when the sample has not
    resolved yet or the URL cannot be produced, so the status route
    degrades to state-only rather than failing.
    """
    if not result_s3_key or not PORTAL_ARTIFACTS_BUCKET:
        return None
    try:
        return s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Key': result_s3_key},
            ExpiresIn=PREVIEW_RESULT_URL_EXPIRY_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — degrade to no URL
        logger.warning(f"Could not presign preview result "
                       f"{result_s3_key}: {e}")
        return None


def _resolve_sample_reference(reference: Any,
                              default_bucket: Optional[str]) -> tuple:
    """`(bucket, key)` for a Sample_Image reference (Req 8.7).

    Resolution happens *before* any scope comparison, so the two
    spellings of the same object classify identically:

        'training-images/a.jpg'                  -> (default_bucket, key)
        's3://uc-bucket/training-images/a.jpg'   -> ('uc-bucket', key)

    A bare key resolves against the Use_Case's dataset bucket. Note
    `_parse_s3_uri` alone is not enough here: fed a bare key it would
    read the first path segment as a bucket name, which would let
    `training-images/a.jpg` masquerade as bucket `training-images` — so
    the `s3://` scheme is tested explicitly first.

    Returns `(None, '')` for anything that is not a non-empty string,
    which the caller reports as an out-of-scope reference rather than
    dereferencing.
    """
    if not isinstance(reference, str) or not reference.strip():
        return None, ''
    candidate = reference.strip()
    if candidate.startswith('s3://'):
        bucket, key = _parse_s3_uri(candidate)
        return (bucket or None), key
    return default_bucket, candidate.lstrip('/')


def _is_reference_in_scope(bucket: Optional[str], key: str,
                           expected_bucket: Optional[str],
                           expected_prefix: str = '') -> bool:
    """Whether a resolved `(bucket, key)` lies inside the expected
    bucket and prefix (Req 8.3, 8.7).

    Applied to already-resolved locations only, so a bare key and its
    `s3://` spelling always reach the same verdict. An empty
    `expected_prefix` means "anywhere in the bucket", which is what the
    Few_Shot_Example check needs (examples live under the Use_Case data
    bucket but not under the dataset prefix).
    """
    if not bucket or not key or not expected_bucket:
        return False
    if bucket != expected_bucket:
        return False
    return key.startswith(expected_prefix or '')


# ---------------------------------------------------------------------------
# POST /labeling-preview/runs (task 8.2 — llm-autolabel-prompt-tuning
# Requirements 1.3, 1.6, 3.5, 3.8, 6.3, 8.1-8.8)
# ---------------------------------------------------------------------------
# The order of operations is fixed and is the whole security model of the
# route:
#
#   1. authorization       @rbac_check([MANAGE_LABELING_JOBS]) scoped to
#                          the *body's* usecase_id, answering one fixed
#                          403 body (Req 8.2, 8.6)
#   2. request validation  every rule evaluated together, so the response
#                          enumerates every violation (Req 8.3-8.5, 6.3)
#   3. scope resolution    each Sample_Image reference resolved to
#                          (bucket, key) *before* being compared against
#                          the Use_Case dataset location (Req 8.7)
#   4. concurrency claim   one conditional write per user and Use_Case
#                          (Req 8.8)
#
# No S3 object is read and no model is invoked on any rejection path: the
# route only ever writes DynamoDB state and fires the async self-invoke.
# The executor (task 9.1) is the first code that touches an image.

# Only the prompt-guided LLM family may be previewed (Req 8.5).
PREVIEW_MODEL_PREFIX = 'llm:'
# Fixed response strings. The 403 body carries no dataset content and no
# existence information, so it reads identically whether or not the
# Use_Case or any referenced object exists (Req 8.2).
PREVIEW_NOT_AUTHORIZED_MESSAGE = 'Not authorized'
PREVIEW_VALIDATION_FAILED_MESSAGE = 'Preview run validation failed'
PREVIEW_IN_PROGRESS_MESSAGE = ('A preview run is already in progress for '
                               'this use case')
# The executor action this function dispatches to when self-invoked.
PREVIEW_EXECUTOR_ACTION = 'execute_preview_run'


def _llm_model_image_limits() -> Dict[str, Any]:
    """The Model_Image_Limit configuration from LLM_MODEL_IMAGE_LIMITS
    (Req 7.1).

    Read per call so the environment stays authoritative. An absent,
    blank, malformed or non-object value resolves to an empty mapping,
    in which case every model resolves the shared default of 20 rather
    than erroring — configuration can never widen or zero the bound.
    """
    raw = (os.environ.get('LLM_MODEL_IMAGE_LIMITS') or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('LLM_MODEL_IMAGE_LIMITS is not valid JSON; using '
                       'the default Model_Image_Limit for every model')
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Settings item key of the persisted Model_Token_Limits mapping — the
# same item data_accounts.py's /token-limits routes read and write, so
# the budget the wizard displays, the budget this route resolves and the
# budget the Auto_Labeler resolves all come from one persisted
# configuration (llm-model-token-and-image-sizing Req 1.6, 1.8).
LLM_MODEL_TOKEN_LIMITS_SETTING_KEY = 'llm_model_token_limits'

# Per-invocation memo of the effective Model_Token_Limits mapping,
# cleared at the top of handler() — see _llm_model_token_limits().
_model_token_limits_cache: Optional[Dict[str, Any]] = None


def _reset_model_token_limits_cache() -> None:
    """Drop the per-invocation Model_Token_Limits memo.

    Called at the top of handler() so the memo never outlives one
    invocation: a warm container serving a mapping cached before an
    administrator's write would contradict Requirement 4.1's "returns
    the persisted mapping" from the user's point of view.
    """
    global _model_token_limits_cache
    _model_token_limits_cache = None


def _decimal_to_native(obj):
    """Decimal-typed numbers from a DynamoDB read as native Python types.

    DynamoDB returns every number as Decimal, and resolve_token_budget
    rejects non-int types by design (llm-model-token-and-image-sizing
    Req 2.8), so the stored mapping is converted before the resolver
    sees any value — otherwise every configured limit would silently
    fall through to the default.
    """
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def _read_stored_model_token_limits() -> Optional[Dict[str, Any]]:
    """The persisted Model_Token_Limits mapping, or None.

    Returns None — meaning "fall back to the environment bootstrap" —
    when the settings table is not configured, the item is absent, the
    read fails, or the item's value is not a mapping. An empty persisted
    mapping is a real mapping and is returned as {} (Req 4.8), not as
    None.
    """
    if not SETTINGS_TABLE:
        return None
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': LLM_MODEL_TOKEN_LIMITS_SETTING_KEY})
    except Exception as e:  # ClientError, table missing, throttling
        logger.warning(f"Could not read model token limits, falling back "
                       f"to the environment bootstrap: {str(e)}")
        return None
    item = response.get('Item')
    if not item:
        return None
    value = item.get('value')
    if not isinstance(value, dict):
        return None
    return _decimal_to_native(value)


def _env_model_token_limits() -> Dict[str, Any]:
    """The LLM_MODEL_TOKEN_LIMITS deploy-time bootstrap mapping.

    An absent, blank, malformed or non-object value resolves to an
    empty mapping, in which case every model resolves
    Model_Token_Limit_Default rather than erroring.
    """
    raw = (os.environ.get('LLM_MODEL_TOKEN_LIMITS') or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('LLM_MODEL_TOKEN_LIMITS is not valid JSON; using '
                       'the default Model_Token_Limit for every model')
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _llm_model_token_limits() -> Dict[str, Any]:
    """The effective Model_Token_Limits mapping (Req 1.6, 1.8).

    The same loader shape data_accounts.py and the Auto_Labeler carry,
    so all three read equal entries for equal persisted configuration.
    Source of truth is the persisted `llm_model_token_limits` settings
    item; when that item is absent, unreadable, or its value is not a
    mapping, the LLM_MODEL_TOKEN_LIMITS environment variable is the
    deploy-time bootstrap.

    WHOLE-MAPPING precedence, never a per-key merge: a merge would let
    an environment entry survive a deletion from the persisted mapping,
    contradicting Req 4.1 ("retain no entry that the submitted mapping
    omits") and Req 4.8 (an empty mapping makes every model resolve the
    default). An empty persisted mapping is therefore honored as empty.

    Memoized PER INVOCATION — a module-level cache keyed by nothing and
    cleared at the top of handler() — which is exactly the span over
    which the resolution must be self-consistent: one preview run start,
    or one executor invocation.
    """
    global _model_token_limits_cache
    if _model_token_limits_cache is None:
        stored = _read_stored_model_token_limits()
        _model_token_limits_cache = (stored if stored is not None
                                     else _env_model_token_limits())
    return _model_token_limits_cache


def _preview_request_body(event: Dict) -> Dict:
    """The request body as a dict; an absent or unparseable body reads as
    an empty object so validation reports the missing elements rather
    than failing with a parse error."""
    try:
        body = json.loads((event or {}).get('body') or '{}')
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


def _inject_preview_usecase_scope(event: Dict, body: Dict) -> None:
    """Make the **body's** usecase_id the @rbac_check scope (Req 8.6).

    The value is written (not `setdefault`) so a query string cannot
    nominate a different Use_Case than the one the run will actually
    execute against — authorization and execution always agree on the
    scope.

    A missing, blank or non-string usecase_id pins the scope to 'global'
    instead: the permission check still runs first (so an unauthorized
    caller gets the same 403 either way and learns nothing), and the
    missing parameter is then reported as a validation error to callers
    who do hold the permission. Pinning also keeps a non-string body
    value out of the permission lookup.
    """
    params = event.get('queryStringParameters') or {}
    raw = body.get('usecase_id')
    params['usecase_id'] = (raw.strip()
                            if isinstance(raw, str) and raw.strip()
                            else 'global')
    event['queryStringParameters'] = params


def _validate_preview_few_shot(examples: List[Any],
                               data_bucket: Optional[str]) -> List[Dict]:
    """Validate the Few_Shot_Example references of an enabled
    Few_Shot_Option (Req 6.3, 8.4).

    Rules: at least one example, at most EXAMPLE_IMAGES_MAX per
    designation, every reference a JPEG or PNG carrying a good/bad
    designation, and every reference resolving inside the Use_Case data
    bucket. The bucket check has no prefix constraint — example images
    live under `labeling-examples/` in the Use_Case data bucket, not
    under the dataset prefix.

    Every violated rule contributes its own entry; nothing
    short-circuits.
    """
    errors: List[Dict] = []
    if not examples:
        errors.append(_validation_error(
            'few_shot',
            'At least one example image is required for the few-shot '
            'examples option'))
        return errors

    counts = {FEW_SHOT_GOOD: 0, FEW_SHOT_BAD: 0}
    for index, example in enumerate(examples):
        ref = example.get('ref') if isinstance(example, dict) else None
        designation = (example.get('designation')
                       if isinstance(example, dict) else None)
        if not isinstance(ref, str) or not ref.strip():
            errors.append(_validation_error(
                'few_shot',
                f'Few-shot example {index} must carry an image reference',
                example_index=index))
            continue
        if designation not in (FEW_SHOT_GOOD, FEW_SHOT_BAD):
            errors.append(_validation_error(
                'few_shot',
                f"Few-shot example image '{ref}' must be designated "
                f"'{FEW_SHOT_GOOD}' or '{FEW_SHOT_BAD}'",
                example_ref=ref))
        else:
            counts[designation] += 1
        if not ref.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
            errors.append(_validation_error(
                'few_shot',
                f"Few-shot example image '{ref}' is not a JPEG or PNG "
                f'image',
                example_ref=ref))
        # Undecidable without a resolved Use_Case data bucket; that case
        # is already reported against usecase_id.
        if data_bucket:
            bucket, key = _resolve_sample_reference(ref, data_bucket)
            if not _is_reference_in_scope(bucket, key, data_bucket):
                errors.append(_validation_error(
                    'few_shot',
                    f"Few-shot example image '{ref}' is outside the use "
                    f"case data bucket '{data_bucket}'",
                    example_ref=ref))

    for designation, count in counts.items():
        if count > EXAMPLE_IMAGES_MAX:
            errors.append(_validation_error(
                'few_shot',
                f'At most {EXAMPLE_IMAGES_MAX} {designation} example '
                f'images can be attached as few-shot examples',
                designation=designation, example_count=count))
    return errors


def _validate_preview_run_request(body: Dict) -> tuple:
    """Validate a Preview_Run request in full and return
    `(config, errors)`.

    Every rule is evaluated — nothing short-circuits on the first
    violation — so one 400 response tells the Job_Creator everything that
    is wrong with the request (Req 8.4). Rules that cannot be decided
    (sample scope with no resolvable dataset bucket) are skipped rather
    than guessed; the reason they are undecidable is itself already
    reported.

    Only DynamoDB/Use_Case metadata is read here: no dataset object is
    fetched and no model is invoked, on this path or on any path that
    leads to a rejection (Req 8.4, 8.5).
    """
    errors: List[Dict] = []

    # --- Use_Case and its dataset bucket -------------------------------
    # Reached only from inside the @rbac_check'd implementation, so an
    # unauthorized caller can never use "use case not found" as an
    # existence oracle (Req 8.2, 8.6).
    raw_usecase_id = body.get('usecase_id')
    usecase_id = (raw_usecase_id.strip()
                  if isinstance(raw_usecase_id, str) else '')
    usecase = None
    dataset_bucket = None
    if not usecase_id:
        errors.append(_validation_error(
            'usecase_id', 'usecase_id is required'))
    else:
        try:
            usecase = get_usecase(usecase_id)
        except ValueError:
            errors.append(_validation_error(
                'usecase_id', 'Use case not found', usecase_id=usecase_id))
        if usecase:
            dataset_bucket = (usecase.get('data_s3_bucket')
                              or usecase.get('s3_bucket'))
            if not dataset_bucket:
                errors.append(_validation_error(
                    'usecase_id', 'Use case has no data bucket configured'))

    # --- model: the llm: family only (Req 8.5) -------------------------
    raw_model = body.get('model')
    model = raw_model if isinstance(raw_model, str) else ''
    model_identifier = None
    if not model.startswith(PREVIEW_MODEL_PREFIX):
        errors.append(_validation_error(
            'model',
            f"Preview runs require a prompt-guided LLM auto-label model "
            f"identifier of the form '{PREVIEW_MODEL_PREFIX}<model_id>'",
            model=model))
    else:
        # Split on the first colon only: model identifiers legitimately
        # contain colons (e.g. 'us.amazon.nova-pro-v1:0').
        candidate = model.split(':', 1)[1]
        identifier_error = validate_model_identifier(candidate)
        if identifier_error:
            errors.append(_validation_error(
                'model', f'Auto-label {identifier_error}', model=model))
        else:
            model_identifier = candidate

    # --- Detection_Prompt (Req 8.4) ------------------------------------
    # Emptiness judged on the stripped value, length on the raw value;
    # the raw string is what the run carries, character-for-character.
    raw_prompt = body.get('detection_prompt')
    detection_prompt = None
    if not isinstance(raw_prompt, str) or not raw_prompt.strip():
        errors.append(_validation_error(
            'detection_prompt',
            'A non-empty detection_prompt is required for a preview run'))
    elif len(raw_prompt) > DETECTION_PROMPT_MAX_LENGTH:
        errors.append(_validation_error(
            'detection_prompt',
            f'detection_prompt must be at most '
            f'{DETECTION_PROMPT_MAX_LENGTH} characters',
            detection_prompt_length=len(raw_prompt)))
    else:
        detection_prompt = raw_prompt

    # --- Labeling_Modality and Label_Set (Req 8.4) ---------------------
    # Deliberately the same rules create_dda_job applies, including the
    # fixed binary Label_Set for Classification, so a request the preview
    # accepts is a request the job creation flow accepts.
    modality = body.get('task_type') or body.get('modality')
    label_set: Optional[List[str]] = None
    if modality not in VALID_MODALITIES:
        errors.append(_validation_error(
            'task_type',
            f"Labeling modality must be one of "
            f"{', '.join(VALID_MODALITIES)}",
            task_type=modality))
    elif modality == 'Classification':
        label_set = list(CLASSIFICATION_LABEL_SET)
    else:
        label_set, label_errors = _validate_label_set(body.get('label_set'))
        errors.extend(label_errors)

    # --- dataset prefix: what the Sample_Images are scoped to ----------
    raw_prefix = body.get('dataset_prefix')
    dataset_prefix = raw_prefix if isinstance(raw_prefix, str) else ''
    if not dataset_prefix:
        errors.append(_validation_error(
            'dataset_prefix',
            'dataset_prefix is required to scope the sample images to the '
            'use case dataset'))

    # --- Sample_Images: count, then resolved scope (Req 8.3, 8.4, 8.7) -
    raw_samples = body.get('sample_images')
    samples = raw_samples if isinstance(raw_samples, list) else []
    if (not isinstance(raw_samples, list)
            or not PREVIEW_SAMPLE_MIN <= len(samples) <= PREVIEW_SAMPLE_MAX):
        errors.append(_validation_error(
            'sample_images',
            f'Between {PREVIEW_SAMPLE_MIN} and {PREVIEW_SAMPLE_MAX} sample '
            f'images must be selected for a preview run',
            sample_count=len(samples)))

    sample_keys: List[str] = []
    for reference in samples:
        # Req 8.7: resolve to (bucket, key) *first*, so 'a/b.jpg' and
        # 's3://bucket/a/b.jpg' always reach the same verdict, and an
        # out-of-scope reference is classified without being dereferenced.
        bucket, key = _resolve_sample_reference(reference, dataset_bucket)
        if not dataset_bucket:
            continue  # undecidable; already reported against usecase_id
        if not _is_reference_in_scope(bucket, key, dataset_bucket,
                                      dataset_prefix):
            printable = (reference if isinstance(reference, str)
                         else str(reference))
            errors.append(_validation_error(
                'sample_images',
                f"Sample image '{printable}' is outside the use case "
                f"dataset location s3://{dataset_bucket}/{dataset_prefix}",
                sample_image=printable))
            continue
        sample_keys.append(key)

    # --- Few_Shot_Option (Req 6.3) -------------------------------------
    few_shot_enabled = False
    few_shot_examples: List[Any] = []
    raw_few_shot = body.get('few_shot')
    if raw_few_shot is None:
        pass  # absent means disabled — the pre-feature request shape
    elif isinstance(raw_few_shot, bool):
        few_shot_enabled = raw_few_shot
    elif isinstance(raw_few_shot, dict):
        few_shot_enabled = bool(raw_few_shot.get('enabled'))
        raw_examples = raw_few_shot.get('examples')
        few_shot_examples = (raw_examples if isinstance(raw_examples, list)
                             else [])
    else:
        errors.append(_validation_error(
            'few_shot',
            "few_shot must be an object like {'enabled': true, "
            "'examples': [...]}"))

    if few_shot_enabled:
        errors.extend(_validate_preview_few_shot(
            few_shot_examples, dataset_bucket))

    # --- Downscale_Setting (llm-model-token-and-image-sizing Req 5.5) --
    # Absent and null both mean Downscale_Off; the only other accepted
    # values are the six Max_Image_Edge integers. The isinstance checks
    # run before the membership test on purpose: bool is an int subclass
    # and 1024.0 == 1024, so a boolean or a whole-valued float would
    # otherwise slip through numeric equality. Strings — including
    # '1024' and 'off' — are rejected with no conversion, which is why
    # Downscale_Off is encoded as null rather than a string sentinel.
    raw_downscale = body.get('downscale_max_edge')
    downscale_max_edge = None
    if raw_downscale is None:
        pass  # Downscale_Off — the pre-feature request shape
    elif (isinstance(raw_downscale, int)
            and not isinstance(raw_downscale, bool)
            and raw_downscale in MAX_IMAGE_EDGE_OPTIONS):
        downscale_max_edge = raw_downscale
    else:
        errors.append(_validation_error(
            'downscale_max_edge',
            f"downscale_max_edge must be null for no downscaling or one "
            f"of {', '.join(str(v) for v in MAX_IMAGE_EDGE_OPTIONS)}",
            downscale_max_edge=raw_downscale))

    # --- Token_Budget_Selection (Req 3.5) -------------------------------
    # Absent means "resolve from the Model_Token_Limits and the default"
    # (Req 3.10); a present value must be a non-boolean integer in
    # [1, MODEL_TOKEN_LIMIT_CEILING]. Unlike downscale_max_edge, null is
    # NOT a valid present value: an empty budget control omits the key
    # entirely, so anything else present-and-invalid — null, a boolean,
    # a digit string, a whole-valued float, an out-of-range integer — is
    # rejected with the accepted range, with no numeric conversion and
    # no clamping.
    raw_budget = body.get('token_budget')
    token_budget_selection = None
    if 'token_budget' not in body:
        pass  # absent — resolve through the mapping and the default
    elif (isinstance(raw_budget, int)
            and not isinstance(raw_budget, bool)
            and 1 <= raw_budget <= MODEL_TOKEN_LIMIT_CEILING):
        token_budget_selection = raw_budget
    else:
        errors.append(_validation_error(
            'token_budget',
            f'token_budget must be a whole number between 1 and '
            f'{MODEL_TOKEN_LIMIT_CEILING}',
            token_budget=raw_budget))

    config = {
        'usecase_id': usecase_id,
        'usecase': usecase,
        'dataset_bucket': dataset_bucket,
        'dataset_prefix': dataset_prefix,
        'model': model,
        'model_identifier': model_identifier,
        'detection_prompt': detection_prompt,
        'task_type': modality,
        'label_set': label_set,
        'sample_keys': sample_keys,
        'few_shot_enabled': few_shot_enabled,
        'few_shot_examples': few_shot_examples,
        'downscale_max_edge': downscale_max_edge,
        'token_budget': token_budget_selection,
    }
    return config, errors


def _invoke_preview_executor(run_id: str, context) -> Optional[str]:
    """Async self-invoke of the Preview_Run executor.

    The function name comes from the invocation context (with the Lambda
    runtime's own AWS_LAMBDA_FUNCTION_NAME as a fallback) rather than
    from a configured environment variable: a stack that passed its own
    function name to itself would be a CloudFormation self-reference.

    Returns None when the executor was started, or a reason string when
    the invoke failed — in which case the caller flips the run to Failed
    and releases the lock, so the wizard's poll surfaces the failure and
    the user is not left holding an in-flight claim (Req 4.7, 8.8).

    An environment with no resolvable function name (unit tests) is
    logged and treated as "not started" without failing the run, the
    same guard `_invoke_labeling_worker` uses.
    """
    function_name = (getattr(context, 'function_name', None)
                     or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))
    if not function_name:
        logger.warning('No Lambda function name available; skipping the '
                       f'preview executor invoke for run {run_id}')
        return None
    try:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=json.dumps({'action': PREVIEW_EXECUTOR_ACTION,
                                'run_id': run_id}),
        )
        return None
    except Exception as e:  # noqa: BLE001 — reported on the run item
        logger.error(f"Failed to async-invoke the preview executor "
                     f"({function_name}) for run {run_id}: {e}")
        return f'The preview run could not be started: {e}'


def start_preview_run(event, context):
    """POST /labeling-preview/runs

    Body (design's Preview_API section):

        {usecase_id, dataset_prefix, model, detection_prompt, task_type,
         label_set, sample_images: [1..5 keys or s3:// URIs],
         few_shot: {enabled, examples: [{ref, designation, position}]},
         downscale_max_edge: null | 512|768|1024|1280|1536|2048,
         token_budget: 1..128000 (absent = mapping + default)}

    Responses:
        202 {run_id, sample_count, status: 'Running'}
        400 {error: 'Preview run validation failed', validation_errors: []}
        403 {error: 'Not authorized'}
        409 {error: 'A preview run is already in progress for this use case'}

    This wrapper exists for Requirement 8.2: the authorization decision
    must be indistinguishable for every unauthorized caller. @rbac_check's
    own 403 body echoes the required permissions and the resolved scope,
    so it is replaced here by one fixed body. The decorator's
    `unauthorized_access` audit event is what records the denial.
    """
    body = _preview_request_body(event)
    _inject_preview_usecase_scope(event, body)
    response = _start_preview_run(event, context)
    if response.get('statusCode') == 403:
        # The implementation below never answers 403 itself, so this can
        # only be the authorization gate.
        return create_response(403, {
            'error': PREVIEW_NOT_AUTHORIZED_MESSAGE})
    return response


@rbac_check([Permission.MANAGE_LABELING_JOBS], allow_global=True)
def _start_preview_run(event, context):
    """The authorized body of POST /labeling-preview/runs.

    Everything here runs *after* the permission check, which is what
    makes "use case not found" and every other validation message safe
    to return (Req 8.6). `allow_global=True` only matters for a request
    that names no Use_Case at all: the check still runs (unauthorized
    callers get the same 403) and the missing parameter is then reported
    as a validation error.
    """
    usecase_id = ''
    user_sub = ''
    lock_claimed = False
    try:
        body = _preview_request_body(event)
        user = get_user_from_event(event)
        user_sub = user['user_id']

        config, errors = _validate_preview_run_request(body)
        if errors:
            # Req 8.4: one response, every violation, nothing touched.
            return create_response(400, {
                'error': PREVIEW_VALIDATION_FAILED_MESSAGE,
                'validation_errors': errors,
            })

        usecase_id = config['usecase_id']
        sample_keys = config['sample_keys']
        sample_count = len(sample_keys)
        run_id = _new_preview_run_id()

        # Req 8.8: one in-flight run per user and Use_Case. The claim is a
        # single conditional write, so concurrent requests cannot both
        # win, and the rejected request reads no object and invokes no
        # model.
        if not _claim_preview_lock(usecase_id, user_sub, run_id,
                                   sample_count):
            return create_response(409, {
                'error': PREVIEW_IN_PROGRESS_MESSAGE})
        lock_claimed = True

        # The attached count is recorded on the run so the status route
        # can report attached/omitted without re-resolving anything. It
        # comes from the same shared selection the Auto_Labeler uses, so
        # the counts describe exactly what the executor will attach
        # (Req 7.2, 7.5, 7.6).
        attached: List[Dict] = []
        omitted: List[Dict] = []
        stored_examples: List[Dict] = []
        if config['few_shot_enabled']:
            stored_examples = _preview_stored_examples(
                config['few_shot_examples'])
            attached, omitted = select_few_shot_examples(
                stored_examples,
                resolve_model_image_limit(config['model_identifier'],
                                          _llm_model_image_limits()))

        # llm-model-token-and-image-sizing Req 1.6, 3.5, 9.5: the
        # Effective_Token_Budget is resolved exactly ONCE, at run start,
        # from the validated Token_Budget_Selection and the same
        # per-model configuration delivery the Auto_Labeler and the
        # model-options listing read. The resolved integer — never the
        # raw selection — is recorded on the RUN item, carried in the
        # audit event and reported by the status route; the executor
        # passes it back in as the selection, and the resolver's
        # idempotence on its own output makes that re-resolution the
        # identity, so the budget sent is provably the budget audited,
        # even if an administrator rewrites the mapping in between.
        token_budget = resolve_token_budget(
            config['model_identifier'], config['token_budget'],
            _llm_model_token_limits())

        _write_preview_run_item(
            run_id=run_id,
            usecase_id=usecase_id,
            created_by=user_sub,
            model=config['model'],
            task_type=config['task_type'],
            label_set=config['label_set'] or [],
            detection_prompt=config['detection_prompt'],
            sample_count=sample_count,
            few_shot_enabled=config['few_shot_enabled'],
            attached_example_count=len(attached),
            omitted_example_count=len(omitted),
            # Recorded so the executor attaches this exact reference set
            # (Req 6.6, 7.6); absent when the option is off.
            few_shot_examples=stored_examples,
            # The validated Downscale_Setting (absent for Downscale_Off)
            # and the resolved Effective_Token_Budget, recorded so the
            # executor applies exactly what was validated and audited
            # here (Req 5.3, 1.6).
            downscale_max_edge=config['downscale_max_edge'],
            token_budget=token_budget,
        )
        # Every requested sample gets its Pending item up front, so the
        # status route answers with one entry per Sample_Image from the
        # moment the run exists (Req 3.5, 4.6).
        _write_preview_sample_items(run_id, sample_keys)

        # Req 3.8: requesting identity, Use_Case, model identifier and
        # Sample_Image count. The two sizing fields ride the SAME event
        # (llm-model-token-and-image-sizing Req 9.5) — still exactly one
        # `preview_run` event per run: the applied Downscale_Setting
        # (null for Downscale_Off) and the resolved
        # Effective_Token_Budget.
        log_audit_event(
            user_id=user_sub,
            action='preview_run',
            resource_type='labeling_preview',
            resource_id=run_id,
            result='success',
            details={
                'usecase_id': usecase_id,
                'model': config['model'],
                'sample_count': sample_count,
                'task_type': config['task_type'],
                'few_shot_enabled': config['few_shot_enabled'],
                'attached_example_count': len(attached),
                'downscale_max_edge': config['downscale_max_edge'],
                'token_budget': token_budget,
            },
        )

        response: Dict[str, Any] = {
            'run_id': run_id,
            'sample_count': sample_count,
            'status': PREVIEW_STATUS_RUNNING,
        }

        # The run is recorded before the executor is started, so a failed
        # invoke is reported on the run itself rather than losing the
        # request: the run flips to Failed with its reason and the lock is
        # released immediately (Req 4.7, 8.8). The response stays 202 —
        # the run exists and the wizard polls it — but reports the actual
        # status instead of claiming Running.
        invoke_error = _invoke_preview_executor(run_id, context)
        if invoke_error:
            _update_preview_run_status(
                run_id, PREVIEW_STATUS_FAILED, run_error=invoke_error)
            _release_preview_lock(usecase_id, user_sub)
            lock_claimed = False
            response['status'] = PREVIEW_STATUS_FAILED
            response['run_error'] = invoke_error

        return create_response(202, response)

    except Exception as e:
        logger.error(f"Error starting preview run: {str(e)}", exc_info=True)
        if lock_claimed and usecase_id and user_sub:
            # Never leave a claim behind for a run that will not execute.
            try:
                _release_preview_lock(usecase_id, user_sub)
            except Exception as release_error:  # noqa: BLE001
                logger.warning(f"Could not release the preview lock for "
                               f"{usecase_id}/{user_sub}: {release_error}")
        return create_response(500, {
            'error': 'Failed to start the preview run'})


# ---------------------------------------------------------------------------
# GET /labeling-preview/runs/{runId} (task 8.3 — llm-autolabel-prompt-tuning
# Requirements 3.5, 4.6, 9.6)
# ---------------------------------------------------------------------------
# The status route is the wizard's only view of a run: it answers the run
# status, the Sample_Image count, the few-shot attached/omitted counts and
# exactly one result entry per requested Sample_Image, in request order,
# for the whole life of the run (Req 3.5, 4.6). Per-sample failure
# category and reason are duplicated onto this response so a failure
# renders without fetching its payload; the payload adds the verbatim raw
# model output (Req 9.6).
#
# One fixed 404 body covers both an unknown run id and a run created by
# another user, so a run belonging to someone else is indistinguishable
# from one that never existed and no run data leaks with the denial.
PREVIEW_RUN_NOT_FOUND_MESSAGE = 'Preview run not found'


def _preview_int(value: Any, default: int = 0) -> int:
    """A DynamoDB numeric attribute as a plain int.

    Number attributes come back as Decimal; the wire shape is integers,
    so they are narrowed here rather than relying on the response
    encoder's float fallback.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _inject_preview_run_usecase_scope(event: Dict, run_id: str) -> None:
    """Make the run's usecase_id the @rbac_check scope for the status
    route.

    Same shape as `_inject_job_usecase_scope`: when the run cannot be
    resolved the scope falls back to 'global' (allow_global) so the
    permission check still runs *first* and the handler answers 404
    afterwards — resolving the scope must never become an existence
    oracle that answers ahead of authorization.
    """
    if not run_id:
        return
    try:
        run = _read_preview_run_item(run_id)
    except Exception as e:  # noqa: BLE001 — scope falls back to global
        logger.warning(f"Could not resolve usecase scope for preview run "
                       f"{run_id}: {e}")
        return
    if run and run.get('usecase_id'):
        params = event.get('queryStringParameters') or {}
        params.setdefault('usecase_id', run['usecase_id'])
        event['queryStringParameters'] = params


def _preview_result_entry(item: Dict) -> Dict[str, Any]:
    """One `results` entry for one Sample_Image item.

    `state` is always present. Failure category/reason and the presigned
    result-payload URL appear only once the sample has resolved, so a
    Pending entry carries no half-populated outcome. The presigned URL is
    generated per request (never stored) and is scoped to that one
    payload object; when it cannot be produced the entry degrades to
    state-only rather than failing the whole response.
    """
    entry: Dict[str, Any] = {
        'index': _preview_sample_index(item['task_id']),
        'sample_key': item.get('sample_key', ''),
        'state': item.get('state', PREVIEW_SAMPLE_PENDING),
    }
    if entry['state'] == PREVIEW_SAMPLE_PENDING:
        return entry

    if item.get('failure_category'):
        entry['failure_category'] = item['failure_category']
    if item.get('failure_reason'):
        entry['failure_reason'] = item['failure_reason']
    if item.get('resolved_at') is not None:
        entry['resolved_at'] = _preview_int(item.get('resolved_at'))
    result_url = _presign_preview_result(item.get('result_s3_key'))
    if result_url:
        entry['result_url'] = result_url
        entry['result_url_expires_in'] = PREVIEW_RESULT_URL_EXPIRY_SECONDS
    return entry


def get_preview_run(event, context):
    """GET /labeling-preview/runs/{runId}

    Responses:
        200 {run_id, status, sample_count, few_shot,
             downscale_max_edge, token_budget, results: [...]}
        403 {error: 'Not authorized'}
        404 {error: 'Preview run not found'}

    The 403 body is flattened for the same reason `start_preview_run`
    flattens it: @rbac_check's own body echoes the required permissions
    and the resolved scope, which would describe the run's Use_Case to a
    caller who is not allowed to see it.
    """
    run_id = ((event or {}).get('pathParameters') or {}).get('runId') or ''
    _inject_preview_run_usecase_scope(event, run_id)
    response = _get_preview_run(event, context)
    if response.get('statusCode') == 403:
        # The implementation below never answers 403 itself.
        return create_response(403, {
            'error': PREVIEW_NOT_AUTHORIZED_MESSAGE})
    return response


@rbac_check([Permission.MANAGE_LABELING_JOBS], allow_global=True)
def _get_preview_run(event, context):
    """The authorized body of GET /labeling-preview/runs/{runId}.

    Ownership is enforced here, after the permission check: a run whose
    `created_by` is not the caller answers the *same* 404 body as an
    unknown run id, carrying no run data at all — the two cases are
    indistinguishable.
    """
    try:
        run_id = ((event or {}).get('pathParameters') or {}).get('runId') or ''
        user_sub = get_user_from_event(event)['user_id']

        run = _read_preview_run_item(run_id) if run_id else None
        if not run or run.get('created_by') != user_sub:
            return create_response(404, {
                'error': PREVIEW_RUN_NOT_FOUND_MESSAGE})

        # One entry per requested Sample_Image, in request order: the
        # items were written Pending up front by the start route and the
        # zero-padded IMAGE#{i:03d} sort key makes DynamoDB's ordering the
        # request ordering (Req 3.5, 4.6).
        results = [_preview_result_entry(item)
                   for item in _read_preview_sample_items(run_id)]

        response: Dict[str, Any] = {
            'run_id': run_id,
            'status': run.get('status', PREVIEW_STATUS_RUNNING),
            'sample_count': _preview_int(run.get('sample_count'),
                                         len(results)),
            'few_shot': {
                'enabled': bool(run.get('few_shot_enabled')),
                # Recorded by the start route from the same
                # select_few_shot_examples call the executor's attachment
                # follows, so these counts always describe what is
                # actually attached (Req 7.5, 7.6).
                'attached': _preview_int(run.get('attached_example_count')),
                'omitted': _preview_int(run.get('omitted_example_count')),
            },
            # The run's applied Downscale_Setting, null for Downscale_Off
            # (llm-model-token-and-image-sizing Req 5.10): the RUN item
            # records the attribute only when a Max_Image_Edge was
            # validated at start, and a pre-feature run means Off too.
            'downscale_max_edge': (
                _preview_int(run['downscale_max_edge'])
                if run.get('downscale_max_edge') is not None else None),
            'results': results,
        }
        # The Effective_Token_Budget resolved and recorded at run start
        # (Req 1.6): always present for runs started after this feature;
        # omitted only for an in-flight pre-feature run, which carried no
        # per-model budget to report.
        if run.get('token_budget') is not None:
            response['token_budget'] = _preview_int(run['token_budget'])
        if run.get('run_error'):
            # A run that failed as a whole (e.g. the executor invoke never
            # landed) reports why, so the wizard can surface the failure
            # and re-enable the controls (Req 4.7).
            response['run_error'] = run['run_error']
        return create_response(200, response)

    except Exception as e:
        logger.error(f"Error reading preview run: {str(e)}", exc_info=True)
        return create_response(500, {
            'error': 'Failed to read the preview run'})


def _handle_preview_action(action: str, event: Dict, context):
    """Non-HTTP action dispatch, entered from `handler` ahead of the HTTP
    routing (task 8.2).

    `POST /labeling-preview/runs` self-invokes this same function with
    `{'action': 'execute_preview_run', 'run_id': ...}`; the executor that
    answers it is task 9.1.
    """
    if action == PREVIEW_EXECUTOR_ACTION:
        return execute_preview_run((event or {}).get('run_id'))

    message = f"Unknown dda_labeling action: {action!r}"
    logger.error(message)
    return {'error': message}


# ---------------------------------------------------------------------------
# Preview_Run executor (task 9.1 — llm-autolabel-prompt-tuning
# Requirements 1.6, 3.1-3.3, 3.5-3.7, 3.9-3.11, 6.6, 6.8, 7.2, 7.6,
# 9.1-9.6)
# ---------------------------------------------------------------------------
# Entered only through the non-HTTP `action` branch of `handler`, from the
# async self-invoke the start route fires. For each Sample_Image, in
# request order:
#
#   1. read the bytes through `get_s3_client_for_bucket` — the same
#      cross-account mechanism, including its single-account direct-access
#      fallback, the Auto_Labeler uses (Req 3.6)
#   2. decode the pixel dimensions from the PNG/JPEG header (Req 3.9)
#   3. read the attached Few_Shot_Example bytes (Req 6.6, 6.8)
#   4. call `dda_llm_prelabel.generate_llm_prelabel` — *the identical
#      call the Auto_Labeler makes*, with the same argument construction,
#      so exactly one Converse request is issued per sample and the
#      Coordinate_Guidance parsing, validation and Pre_Label conversion
#      are literally the same code (Req 3.1, 3.2, 3.3, 3.10, 3.11)
#
# Each outcome is written the moment it resolves — payload JSON to
# `labeling-previews/{usecase_id}/{run_id}/{i}.json`, state plus category
# and reason onto the `IMAGE#{i}` item — so the wizard's polling renders
# progressively and a per-sample failure never disturbs another sample
# (Req 3.5, 3.7). Steps 1-3 fail without any invocation, which is what
# makes `image_access_failure`, `unsupported_image_content` and
# `unreadable_example_image` "zero model invocations" categories
# (Req 3.9, 6.8, 9.4, 9.5).
#
# The run reaches Completed after the last sample even when every sample
# failed — "Failed" is reserved for a run that could produce no per-sample
# results at all — and the in-flight lock is released on every terminal
# path, including an unexpected exception (Req 8.8).
#
# Nothing here writes a Labeling_Job record, a Task_Assignment item, a
# pipeline Pre_Label artifact under `labeling/{usecase_id}/`, or a labeler
# notification (Req 1.6, 3.5).

# The three categories the executor produces itself, before any model
# invocation. The other three come from `LlmPrelabelError.category`
# unchanged, so their reasons are the strings the Auto_Labeler records.
PREVIEW_CATEGORY_IMAGE_ACCESS = 'image_access_failure'
PREVIEW_CATEGORY_UNSUPPORTED_IMAGE = 'unsupported_image_content'
PREVIEW_CATEGORY_UNREADABLE_EXAMPLE = 'unreadable_example_image'
# The Auto_Labeler's reason for an undecodable image, verbatim.
PREVIEW_UNSUPPORTED_IMAGE_REASON = (
    'unsupported image content: could not determine image dimensions for '
    'coordinate guidance')


class PreviewSampleFailure(Exception):
    """One Sample_Image's failure, carrying exactly one category from
    PREVIEW_FAILURE_CATEGORIES (Req 9.6).

    Raised by every step of `_run_preview_sample`, so a sample always
    resolves to precisely one category with one reason and, when a model
    response was received, the raw model output character-for-character
    (Req 9.3).
    """

    def __init__(self, category: str, reason: str,
                 raw_model_output: Optional[str] = None):
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.raw_model_output = raw_model_output


def _preview_image_dimensions(image_bytes: bytes) -> Optional[tuple]:
    """(width, height) parsed from PNG IHDR / JPEG SOF headers, or None.

    A thin delegation to `dda_llm_image.declared_dimensions` — the same
    algorithm this function has always used, relocated verbatim to the
    shared layer so exactly one copy exists and the preview decodes
    dimensions the same way the Auto_Labeler does by construction
    (llm-model-token-and-image-sizing Req 7.6; llm-autolabel-prompt-tuning
    Req 3.1). Accepts exactly the inputs it accepted before and stays
    dependency-free (no Pillow).
    """
    return declared_dimensions(image_bytes)


def _preview_s3_client(clients: Dict[str, Any], usecase: Dict, bucket: str):
    """Cached S3 client for one bucket, through the Use_Case's
    cross-account role with the single-account direct-access fallback
    (Req 3.6).

    `get_s3_client_for_bucket` is the same entry point the Auto_Labeler's
    `_dataset_s3_client` uses, so the preview reads Sample_Images and
    example images over exactly the mechanism labeling time reads them
    over — including the fallback. Clients are cached per bucket for the
    life of the run: credentials, never bytes, so per-sample reads stay
    independent.
    """
    if bucket not in clients:
        clients[bucket] = get_s3_client_for_bucket(
            usecase, bucket, 'dda-labeling-preview')
    return clients[bucket]


def _read_preview_object(clients: Dict[str, Any], usecase: Dict,
                         bucket: str, key: str) -> bytes:
    """One object's bytes, or raise the caller's failure category."""
    return _preview_s3_client(clients, usecase, bucket).get_object(
        Bucket=bucket, Key=key)['Body'].read()


def _resolve_preview_few_shot_images(run: Dict, clients: Dict[str, Any],
                                     usecase: Dict,
                                     dataset_bucket: Optional[str],
                                     model_identifier: str) -> tuple:
    """`(images, model_image_limit)` for one Sample_Image's request
    (Req 6.6, 6.8, 7.2, 7.6).

    The mirror of `dda_autolabel_worker._resolve_few_shot_images`: the
    same `select_few_shot_examples(stored, resolve_model_image_limit(...))`
    selection over the same stored reference shape, the same
    `{'bytes', 'format', 'designation'}` image dicts in the same order,
    and only the attached references are read — an omitted reference is
    never fetched. The one difference is where the references come from:
    the RUN item for a preview, the job record at labeling time.

    A disabled option (or a run with no recorded references) returns
    `([], limit)`, which is the pre-feature request shape (Req 10.2).

    Raises:
        PreviewSampleFailure: `unreadable_example_image`, naming the
            reference, when an attached example cannot be read. It fails
            only this Sample_Image (Req 6.8).
    """
    limit = resolve_model_image_limit(model_identifier,
                                      _llm_model_image_limits())
    if not run.get('few_shot_enabled'):
        return [], limit
    stored = run.get('few_shot_examples')
    if not isinstance(stored, list):
        return [], limit
    candidates = [example for example in stored
                  if isinstance(example, dict)
                  and isinstance(example.get('ref'), str)
                  and example['ref']]
    if not candidates:
        return [], limit

    attached, _omitted = select_few_shot_examples(candidates, limit)

    images: List[Dict] = []
    for example in attached:
        ref = example['ref']
        # Resolved the same way the request validation resolved it, so an
        # example is read from the location it was scope-checked against
        # (Req 8.7).
        bucket, key = _resolve_sample_reference(ref, dataset_bucket)
        if not bucket or not key:
            raise PreviewSampleFailure(
                PREVIEW_CATEGORY_UNREADABLE_EXAMPLE,
                f'few-shot example image {ref} is not accessible: '
                f'the reference could not be resolved to an S3 object')
        try:
            body = _read_preview_object(clients, usecase, bucket, key)
        except Exception as exc:  # noqa: BLE001 — unreadable example (6.8)
            raise PreviewSampleFailure(
                PREVIEW_CATEGORY_UNREADABLE_EXAMPLE,
                f'few-shot example image {ref} is not accessible: '
                f'{exc}') from exc
        images.append({
            'bytes': body,
            'format': image_format_for_key(key),
            'designation': example.get('designation'),
        })
    return images, limit


def _run_preview_sample(run: Dict, clients: Dict[str, Any], usecase: Dict,
                        dataset_bucket: str, sample_key: str) -> tuple:
    """Produce one Sample_Image's Pre_Label, or raise its single
    categorized failure.

    Total by construction: every step maps its own errors onto exactly
    one category, so a sample can never resolve without a category and
    can never carry two (Req 9.6).

    Returns:
        `(prelabel, (source_width, source_height), (sent_width,
        sent_height))` — the Pre_Label first, as always, then the
        Source_Dimensions, then the Sent_Dimensions of the image
        actually sent to the model, straight from the shared
        chokepoint's result (llm-model-token-and-image-sizing Req 5.10,
        7.1). With no Downscale_Setting the two pairs are equal.
    """
    # 1. Sample_Image bytes — cross-account with the direct fallback.
    try:
        image_bytes = _read_preview_object(
            clients, usecase, dataset_bucket, sample_key)
    except Exception as exc:  # noqa: BLE001 — Req 9.4, no invocation
        raise PreviewSampleFailure(
            PREVIEW_CATEGORY_IMAGE_ACCESS,
            f'image s3://{dataset_bucket}/{sample_key} is not '
            f'accessible: {exc}') from exc

    # 2. Pixel dimensions: they are part of the prompt, so an image whose
    #    header cannot be read fails before any invocation (Req 9.5).
    try:
        dimensions = _preview_image_dimensions(image_bytes)
    except Exception:  # noqa: BLE001 — undecodable header
        dimensions = None
    if not dimensions:
        raise PreviewSampleFailure(
            PREVIEW_CATEGORY_UNSUPPORTED_IMAGE,
            PREVIEW_UNSUPPORTED_IMAGE_REASON)
    width, height = dimensions

    model = run.get('model') or ''
    # The Converse modelId is the part after the `llm:` prefix, split on
    # the first colon only — model identifiers legitimately contain
    # colons (e.g. 'us.amazon.nova-pro-v1:0').
    model_identifier = model.split(':', 1)[1] if ':' in model else model

    # 3. Few_Shot_Examples, read *after* the target image so an
    #    unreadable example can never mask an unreadable target — the
    #    Auto_Labeler's ordering (Req 6.8).
    few_shot_images, model_image_limit = _resolve_preview_few_shot_images(
        run, clients, usecase, dataset_bucket, model_identifier)

    # 4. The identical call the Auto_Labeler makes (Req 3.1, 3.2). The
    #    Bedrock client seam is rebound onto the shared module for the
    #    same reason the worker rebinds it: this module stays the single
    #    place a client is obtained from.
    #
    #    The sizing inputs are the run's recorded values, passed straight
    #    through to the shared chokepoint — the executor carries no
    #    sizing logic of its own (llm-model-token-and-image-sizing
    #    Req 5.4, 6.1). DynamoDB returns numbers as Decimal, so both
    #    values pass through _decimal_to_native before the total-and-safe
    #    resolvers see them, exactly as the Auto_Labeler reads the job
    #    record's. `downscale_max_edge` is absent for Downscale_Off and
    #    a malformed recorded value degrades to Downscale_Off with no
    #    failure (Req 5.9, 5.12). `token_budget` is the
    #    Effective_Token_Budget the start route already resolved and
    #    audited; passing it back in as the selection makes re-resolution
    #    the identity, so the budget sent is provably the budget audited,
    #    even if an administrator rewrote the mapping in between
    #    (Req 5.10, 1.6). The mapping itself comes from the same
    #    per-invocation loader the start route and the model-options
    #    listing read (Req 1.8).
    label_set = [str(label) for label in (run.get('label_set') or [])]
    dda_llm_prelabel.get_bedrock_client = get_bedrock_client
    try:
        result = generate_llm_prelabel(
            model_identifier=model_identifier,
            modality=run.get('task_type'),
            label_set=label_set,
            detection_prompt=run.get('detection_prompt') or '',
            # Per_Label_Prompts are a skip-verification job setting; a
            # Preview_Run has no job, so it never carries them.
            per_label_prompts=None,
            image_bytes=image_bytes,
            image_key=sample_key,
            width=width,
            height=height,
            few_shot_images=few_shot_images,
            model_image_limit=model_image_limit,
            downscale_setting=normalize_downscale_setting(
                _decimal_to_native(run.get('downscale_max_edge'))),
            token_budget_selection=_decimal_to_native(
                run.get('token_budget')),
            model_token_limits=_llm_model_token_limits(),
        )
    except LlmPrelabelError as exc:
        # 'timeout' | 'model_error' | 'unusable_model_output' from the
        # invocation and its output, plus the shared chokepoint's
        # 'unsupported_image_content' (a target the Image_Downscaler
        # refuses) and 'unreadable_example_image' (a refused attached
        # example) — every one a pre-existing category from
        # PREVIEW_FAILURE_CATEGORIES, so this translation stays
        # category-preserving with no new category and the reason string
        # is the one the Auto_Labeler records for the same failure,
        # character-for-character (Req 3.10, 3.11, 9.1, 9.2, 9.3;
        # llm-model-token-and-image-sizing Req 9.1, 9.3, 8.5). The two
        # downscale categories imply zero invocations for this sample.
        raise PreviewSampleFailure(exc.category, exc.reason,
                                   raw_model_output=exc.raw_text) from exc
    except Exception as exc:  # noqa: BLE001 — Req 9.1: still one category
        raise PreviewSampleFailure(
            'model_error', f'model error: {exc}') from exc
    # The chokepoint returns the Pre_Label plus the Sent_Dimensions
    # (llm-model-token-and-image-sizing Req 5.10). The seam contract for
    # a stand-in bound to this module's `generate_llm_prelabel` binding
    # is looser: a bare Pre_Label means "sent equals source", which is
    # exactly the pre-feature meaning.
    if isinstance(result, LlmPrelabelResult):
        prelabel = result.prelabel
        sent_dimensions = (result.sent_width, result.sent_height)
    else:
        prelabel = result
        sent_dimensions = (width, height)
    return prelabel, (width, height), sent_dimensions


def _resolve_preview_dataset_location(usecase_id: str) -> tuple:
    """`(usecase, dataset_bucket, error)` for the run's Use_Case.

    Resolved once per run. When it cannot be resolved, `error` explains
    why and every sample resolves as an `image_access_failure` naming it
    — the run still returns one categorized outcome per requested
    Sample_Image rather than collapsing into a run-level failure
    (Req 3.5, 9.4).
    """
    try:
        usecase = get_usecase(usecase_id)
    except Exception as exc:  # noqa: BLE001 — unknown / unreadable
        return None, None, f'use case {usecase_id!r} could not be read: {exc}'
    dataset_bucket = (usecase.get('data_s3_bucket')
                      or usecase.get('s3_bucket'))
    if not dataset_bucket:
        return usecase, None, (f'use case {usecase_id!r} has no data bucket '
                               f'configured')
    return usecase, dataset_bucket, None


def execute_preview_run(run_id: Optional[str]) -> Dict[str, Any]:
    """Execute a Preview_Run's Sample_Images (task 9.1).

    Sequential by design: one Sample_Image at a time, each resolved and
    written before the next begins, so the per-sample bound of 120 s
    composes into the run bound the lock TTL is derived from and the
    wizard sees results appear one by one.

    Idempotent against a duplicated async delivery: a run that is no
    longer Running is not re-executed, and an already-resolved sample is
    not re-invoked, so no Sample_Image can ever receive a second model
    invocation (Req 3.1).
    """
    run = _read_preview_run_item(run_id) if run_id else None
    if not run:
        logger.error(f"Preview run {run_id!r} not found; nothing to execute")
        return {'run_id': run_id, 'action': PREVIEW_EXECUTOR_ACTION,
                'error': 'preview run not found'}

    usecase_id = run.get('usecase_id') or ''
    user_sub = run.get('created_by') or ''
    status = run.get('status')
    if status != PREVIEW_STATUS_RUNNING:
        logger.warning(f"Preview run {run_id} is {status}, not "
                       f"{PREVIEW_STATUS_RUNNING}; skipping execution")
        return {'run_id': run_id, 'action': PREVIEW_EXECUTOR_ACTION,
                'status': status, 'skipped': True}

    succeeded = 0
    failed = 0
    try:
        samples = _read_preview_sample_items(run_id)
        usecase, dataset_bucket, location_error = (
            _resolve_preview_dataset_location(usecase_id))
        # Credentials cache, per bucket, for the life of the run.
        clients: Dict[str, Any] = {}

        # The run's recorded Downscale_Setting, normalized once for the
        # whole run (llm-model-token-and-image-sizing Req 5.10): None
        # for Downscale_Off — the attribute is absent on an Off run's
        # item, and a malformed recorded value degrades to Off the same
        # way the Auto_Labeler degrades the job record's (Req 5.12).
        # This is the same value `_run_preview_sample` hands the shared
        # chokepoint, so the sizing report describes exactly the setting
        # that was applied.
        run_downscale_max_edge = normalize_downscale_setting(
            _decimal_to_native(run.get('downscale_max_edge')))

        for item in samples:
            index = _preview_sample_index(item['task_id'])
            sample_key = item.get('sample_key') or ''
            if item.get('state') != PREVIEW_SAMPLE_PENDING:
                continue

            payload: Dict[str, Any]
            failure: Optional[PreviewSampleFailure] = None
            try:
                if location_error:
                    raise PreviewSampleFailure(
                        PREVIEW_CATEGORY_IMAGE_ACCESS,
                        f'image {sample_key} is not accessible: '
                        f'{location_error}')
                # The Source_Dimensions and the Sent_Dimensions both
                # ride along from the shared chokepoint; on a run with a
                # Downscale_Setting the payload carries the sizing
                # report beside the pre-feature fields
                # (llm-model-token-and-image-sizing Req 5.4, 5.10), with
                # `image_width` / `image_height` keeping their meaning
                # as the Source_Dimensions (Req 7.7).
                prelabel, source_dimensions, sent_dimensions = (
                    _run_preview_sample(
                        run, clients, usecase, dataset_bucket, sample_key))
                width, height = source_dimensions
                payload = _preview_success_payload(
                    sample_key, prelabel, width, height,
                    sent_dimensions=sent_dimensions,
                    downscale_max_edge=run_downscale_max_edge)
            except PreviewSampleFailure as sample_failure:
                failure = sample_failure
                payload = _preview_failure_payload(
                    sample_key, sample_failure.category,
                    sample_failure.reason,
                    raw_model_output=sample_failure.raw_model_output)

            # Written immediately, payload first: the item's
            # `result_s3_key` never points at an object that is not there
            # yet (Req 3.5, 3.7).
            result_key = _write_preview_result_payload(
                usecase_id, run_id, index, payload)
            if failure is None:
                succeeded += 1
                _update_preview_sample_state(
                    run_id, index, PREVIEW_SAMPLE_SUCCEEDED,
                    result_s3_key=result_key)
            else:
                failed += 1
                _update_preview_sample_state(
                    run_id, index, PREVIEW_SAMPLE_FAILED,
                    failure_category=failure.category,
                    failure_reason=failure.reason,
                    result_s3_key=result_key)

        # Req 3.7: the run completes once every sample has an outcome,
        # including a run in which every sample failed.
        _update_preview_run_status(run_id, PREVIEW_STATUS_COMPLETED)
        return {'run_id': run_id, 'action': PREVIEW_EXECUTOR_ACTION,
                'status': PREVIEW_STATUS_COMPLETED,
                'sample_count': len(samples),
                'succeeded': succeeded, 'failed': failed}

    except Exception as e:
        # Only a failure that prevents per-sample results at all reaches
        # here (e.g. a DynamoDB or artifacts-bucket write failure); the
        # per-sample outcomes are categorized above and never abort the
        # loop.
        logger.error(f"Preview run {run_id} failed: {str(e)}", exc_info=True)
        try:
            _update_preview_run_status(
                run_id, PREVIEW_STATUS_FAILED,
                run_error=f'The preview run could not be completed: {e}')
        except Exception as status_error:  # noqa: BLE001
            logger.warning(f"Could not mark preview run {run_id} failed: "
                           f"{status_error}")
        return {'run_id': run_id, 'action': PREVIEW_EXECUTOR_ACTION,
                'status': PREVIEW_STATUS_FAILED, 'error': str(e),
                'succeeded': succeeded, 'failed': failed}

    finally:
        # Every terminal path releases the claim, including an unexpected
        # exception, so a user is never left unable to start a new run
        # (Req 8.8). The release is unconditional and a no-op when the
        # lock has already expired or been reaped.
        if usecase_id and user_sub:
            try:
                _release_preview_lock(usecase_id, user_sub)
            except Exception as release_error:  # noqa: BLE001
                logger.warning(f"Could not release the preview lock for "
                               f"{usecase_id}/{user_sub}: {release_error}")
