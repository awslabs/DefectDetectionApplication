"""
DDA Data Labeling backend (dda-data-labeling).

This handler serves the portal-native labeling APIs registered in the
DdaLabelingApiStack. Task 4.1 implements the Labeling_Team management
routes (Requirements 3.1-3.8); task 5.3 adds `create_dda_job` (invoked
by labeling.py's backend switch, Requirements 4.1-4.11, 8.1, 8.8,
9.1-9.3, 11.3, 11.7, 12.1-12.3); later tasks add the labeler APIs and
the skip-verification admin review on this same handler.

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
AUTO_LABEL_MODEL_MODALITIES = {
    'sam': ('Segmentation', 'ObjectDetection'),
    'bedrock': ('Classification', 'ObjectDetection'),
}
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
    try:
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
    included (Req 4.5). Returns (image keys, invalid objects) where each
    invalid object identifies the offending key and reason (Req 4.7)."""
    images: List[str] = []
    invalid: List[Dict] = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('/'):
                continue  # folder placeholder objects carry no image data
            if key.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
                images.append(key)
            else:
                invalid.append({'key': key, 'reason': 'unsupported_format'})
    return images, invalid


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
        if auto_label_enabled:
            if auto_label_model == 'sam':
                model_family = 'sam'
            elif (isinstance(auto_label_model, str)
                    and auto_label_model.startswith('bedrock:')
                    and auto_label_model.split(':', 1)[1]):
                model_family = 'bedrock'
            else:
                model_family = None
                errors.append(_validation_error(
                    'auto_label',
                    f"Auto-label model must be 'sam' or "
                    f"'bedrock:<model_id>'",
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
            images, invalid_objects = _enumerate_dataset_images(
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

        if invalid_objects:
            return create_response(400, {
                'error': 'Dataset prefix contains objects that are not '
                         'supported JPEG/PNG images',
                'dataset_bucket': dataset_bucket,
                'dataset_prefix': dataset_prefix,
                'invalid_objects': invalid_objects,
            })

        if not images:
            return create_response(400, {
                'error': f"No image objects found under dataset prefix "
                         f"'{dataset_prefix}'",
                'dataset_bucket': dataset_bucket,
                'dataset_prefix': dataset_prefix,
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
            'instructions': instructions,
            'example_images': example_images,
            'auto_label': {
                'enabled': auto_label_enabled,
                **({'model': auto_label_model} if auto_label_enabled else {}),
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
