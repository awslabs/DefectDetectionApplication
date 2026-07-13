"""
Workflow_Test_Runner Step Functions task handlers (Workflow Manager)

Lambda steps of the test-run state machine
Validate -> Compile (x86_64, simulation=true) -> RunSandbox (Fargate) ->
CollectResults (design section 10, task 11.2).

Each state invokes this handler with {"step": <name>, "input": <state
machine input>} where the input is the execution input started by
workflow_testing.py:

    {test_run_id, workflow_id, workflow_version, usecase_id, dataset_id,
     dataset_s3_prefix, definition_s3_key, results_s3_key,
     artifacts_bucket, target_arch: "x86_64", simulation: true}

Steps:
    validate         Parse the stored Workflow_Definition and run all
                     Workflow_Validator checks (12.4). Errors short-circuit:
                     each is recorded with its node/connection identifier in
                     the results document, the TestRuns item is marked
                     failed, and the pipeline is never executed (12.12).
    compile          Workflow_Compiler for the target architecture with
                     simulation=true (12.4, 12.6); uploads the Compiled
                     Pipeline Document next to the results for the sandbox
                     task. Compile errors short-circuit exactly like
                     validation errors (12.12).
    collect          Read the per-node results the sandbox flushed
                     incrementally to S3 and mark the run completed, or
                     failed with the failing node identified (12.10).
    record_timeout   The 10-minute execution timeout stopped the sandbox
                     task: mark the run failed with a timeout indication;
                     partial per-node results already in S3 are retained
                     untouched (12.13).
    record_failure   The sandbox task (or an internal step) failed: mark the
                     run failed; partial results are retained (12.10).

This module is standalone (workflow_core layer only, no shared_utils) so
the test-runner stack can deploy it with a minimal role: TestRuns table
read/write and portal-artifacts S3 read/write. It has no Greengrass or
device permissions (12.9).
"""
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# workflow_core ships as a Lambda layer under /opt/python
sys.path.append('/opt/python')
from workflow_core.compiler import CompileContext, compile as compile_workflow
from workflow_core.serializer import parse as parse_definition
from workflow_core.validator import SEVERITY_ERROR, validate as run_validator

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

TEST_RUNS_TABLE = os.environ.get('TEST_RUNS_TABLE')

#: Message recorded when the 10-minute limit terminates the run (12.13).
TIMEOUT_MESSAGE = 'Test run exceeded the 10 minute execution limit'

# Human-readable progress entries appended to the TestRuns item's
# `progress` list attribute as the run advances through the state machine,
# so the portal can show what the run is currently doing while the user
# waits (polled via GET /test-runs/{id}). Each entry is
# {at: <epoch ms>, message: <text>}, most recent last.
PROGRESS_VALIDATING = 'Validating the workflow definition'
PROGRESS_VALIDATION_PASSED = 'Validation passed'
PROGRESS_COMPILE_SUCCEEDED = 'Compilation succeeded'
# The sandbox container start has no Lambda step of its own (the Fargate
# RunSandbox state follows the compile step directly), so the successful
# compile step appends this entry last.
PROGRESS_STARTING_SANDBOX = 'Starting the sandbox container...'
PROGRESS_COLLECTING = 'Collecting per-node results'
PROGRESS_COMPLETED = 'Test run completed'


def compile_progress_message(target_arch: str, simulation: bool) -> str:
    """Progress entry recorded while the Workflow_Compiler runs."""
    return 'Compiling for {0} ({1} mode)'.format(
        target_arch, 'simulation' if simulation else 'hardware')


def failure_progress_message(message: str) -> str:
    """Progress entry recorded when the run is marked failed."""
    return 'Test run failed: {0}'.format(message)


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def compiled_document_key(results_s3_key: str) -> str:
    """The Compiled Pipeline Document lives next to the results document:
    .../test-runs/{test_run_id}/compiled_pipeline.json"""
    prefix = results_s3_key.rsplit('/', 1)[0] if '/' in results_s3_key else ''
    return (prefix + '/' if prefix else '') + 'compiled_pipeline.json'


def load_definition(bucket: str, key: str) -> str:
    """Fetch the stored Workflow_Definition JSON text"""
    response = s3.get_object(Bucket=bucket, Key=key)
    return response['Body'].read().decode('utf-8')


def write_results_document(bucket: str, key: str, records: List[Dict]) -> None:
    """Write the per-node results document ({"nodes": [...]}, the shape
    workflow_testing.load_node_results consumes)."""
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps({'nodes': records}, indent=2).encode('utf-8'),
        ContentType='application/json',
    )


def error_records(errors: List[Dict]) -> List[Dict]:
    """Per-node/connection error records in the per-node result shape
    {nodeId, status, outputs, stubActivity, error} (12.7, 12.12)."""
    return [
        {
            'nodeId': error.get('nodeId'),
            'connectionId': error.get('connectionId'),
            'status': 'error',
            'outputs': [],
            'stubActivity': [],
            'error': {
                'code': error.get('code'),
                'message': error.get('message'),
            },
        }
        for error in errors
    ]


def append_run_progress(test_run_id: str, *messages: str) -> None:
    """Append {at, message} entries to the TestRuns item's `progress`
    list (created on first use). Purely informational and additive: the
    run's status semantics are untouched, and a progress write failure
    never fails the step itself."""
    entries = [{'at': now_ms(), 'message': message} for message in messages]
    if not entries:
        return
    try:
        dynamodb.Table(TEST_RUNS_TABLE).update_item(
            Key={'test_run_id': test_run_id},
            UpdateExpression='SET progress = '
                             'list_append(if_not_exists(progress, :empty), :entries)',
            ExpressionAttributeValues={':empty': [], ':entries': entries},
        )
    except ClientError as e:
        logger.warning('Could not record progress for run %s: %s',
                       test_run_id, str(e))


def mark_run_failed(test_run_id: str, message: str,
                    node_id: Optional[str] = None,
                    timeout: bool = False) -> None:
    """Mark the TestRuns item failed with the failure record
    {nodeId, message, timeout} (design data model)."""
    dynamodb.Table(TEST_RUNS_TABLE).update_item(
        Key={'test_run_id': test_run_id},
        UpdateExpression='SET #s = :status, finished_at = :finished, failure = :failure',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':status': 'failed',
            ':finished': now_ms(),
            ':failure': {'nodeId': node_id, 'message': message, 'timeout': timeout},
        },
    )
    append_run_progress(test_run_id, failure_progress_message(message))


def mark_run_completed(test_run_id: str) -> None:
    """Mark the TestRuns item completed"""
    dynamodb.Table(TEST_RUNS_TABLE).update_item(
        Key={'test_run_id': test_run_id},
        UpdateExpression='SET #s = :status, finished_at = :finished, failure = :failure',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':status': 'completed',
            ':finished': now_ms(),
            ':failure': None,
        },
    )
    append_run_progress(test_run_id, PROGRESS_COMPLETED)


def record_short_circuit(inp: Dict, errors: List[Dict], summary: str) -> None:
    """Validation/compilation errors short-circuit the run: write the
    per-node/connection error records and mark the run failed without
    executing the pipeline (12.12)."""
    records = error_records(errors)
    write_results_document(inp['artifacts_bucket'], inp['results_s3_key'], records)
    first_node_id = next((e.get('nodeId') for e in errors if e.get('nodeId')), None)
    mark_run_failed(inp['test_run_id'], summary, node_id=first_node_id)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_validate(inp: Dict) -> Dict:
    """Validate: parse the definition and run all validator checks (12.4).
    Errors are recorded with node/connection ids and fail the run (12.12)."""
    append_run_progress(inp['test_run_id'], PROGRESS_VALIDATING)
    document = load_definition(inp['artifacts_bucket'], inp['definition_s3_key'])

    result = parse_definition(document)
    if not result.ok:
        error = result.error
        errors = [{'code': error.code, 'message': str(error),
                   'nodeId': None, 'connectionId': None}]
        record_short_circuit(inp, errors,
                             'Workflow definition could not be parsed: ' + str(error))
        return {'ok': False, 'stage': 'parse', 'errors': errors}

    findings = run_validator(result.graph)
    errors = [f.to_dict() for f in findings if f.severity == SEVERITY_ERROR]
    if errors:
        record_short_circuit(
            inp, errors,
            'Workflow validation reported {0} error(s); the pipeline was not '
            'executed'.format(len(errors)))
        return {'ok': False, 'stage': 'validate', 'errors': errors}

    append_run_progress(inp['test_run_id'], PROGRESS_VALIDATION_PASSED)
    warnings = sum(1 for f in findings if f.severity != SEVERITY_ERROR)
    return {'ok': True, 'warnings': warnings}


def step_compile(inp: Dict) -> Dict:
    """Compile for the target architecture (x86_64) with simulation=true
    (12.4, 12.6) and stage the Compiled Pipeline Document for the sandbox.
    Compile errors short-circuit the run (12.12)."""
    target_arch = inp.get('target_arch') or 'x86_64'
    simulation = bool(inp.get('simulation', True))
    append_run_progress(inp['test_run_id'],
                        compile_progress_message(target_arch, simulation))
    bucket = inp['artifacts_bucket']
    document = load_definition(bucket, inp['definition_s3_key'])

    result = parse_definition(document)
    if not result.ok:
        # Validate ran first; a parse failure here means the stored
        # definition changed between steps. Treat it like a compile error.
        error = result.error
        errors = [{'code': error.code, 'message': str(error),
                   'nodeId': None, 'connectionId': None}]
        record_short_circuit(inp, errors,
                             'Workflow definition could not be parsed: ' + str(error))
        return {'ok': False, 'stage': 'parse', 'errors': errors}

    context = CompileContext(
        workflow_id=str(inp.get('workflow_id') or ''),
        workflow_version=str(inp.get('workflow_version') or ''),
    )
    outcome = compile_workflow(
        result.graph,
        target_arch,
        context=context,
        simulation=simulation,
    )

    if isinstance(outcome, list):
        errors = [e.to_dict() for e in outcome]
        record_short_circuit(
            inp, errors,
            'Workflow compilation reported {0} error(s); the pipeline was not '
            'executed'.format(len(errors)))
        return {'ok': False, 'stage': 'compile', 'errors': errors}

    key = compiled_document_key(inp['results_s3_key'])
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=outcome.to_json().encode('utf-8'),
        ContentType='application/json',
    )
    # The next state is the Fargate sandbox task (no Lambda runs there),
    # so the sandbox-start entry is appended here on compile success.
    append_run_progress(inp['test_run_id'],
                        PROGRESS_COMPILE_SUCCEEDED, PROGRESS_STARTING_SANDBOX)
    return {'ok': True, 'compiled_s3_key': key}


def step_collect(inp: Dict) -> Dict:
    """CollectResults: read the per-node results the sandbox flushed to S3
    and finalize the run status (12.7, 12.10)."""
    append_run_progress(inp['test_run_id'], PROGRESS_COLLECTING)
    records: List[Dict] = []
    try:
        response = s3.get_object(Bucket=inp['artifacts_bucket'],
                                 Key=inp['results_s3_key'])
        document = json.loads(response['Body'].read().decode('utf-8'))
        if isinstance(document, dict) and isinstance(document.get('nodes'), list):
            records = document['nodes']
        elif isinstance(document, list):
            records = document
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') not in ('NoSuchKey', '404'):
            raise
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error('Malformed results document %s: %s',
                     inp['results_s3_key'], str(e))

    failing = next(
        (r for r in records
         if isinstance(r, dict) and (r.get('status') in ('failed', 'error') or r.get('error'))),
        None,
    )
    if failing is not None:
        error = failing.get('error')
        message = (error.get('message') if isinstance(error, dict) else error) \
            or 'Pipeline execution failed'
        mark_run_failed(inp['test_run_id'], str(message),
                        node_id=failing.get('nodeId'))
        return {'status': 'failed', 'node_count': len(records),
                'failing_node_id': failing.get('nodeId')}

    mark_run_completed(inp['test_run_id'])
    return {'status': 'completed', 'node_count': len(records)}


def step_record_timeout(inp: Dict) -> Dict:
    """The 10-minute execution timeout stopped the sandbox task: mark the
    run failed-with-timeout. Partial per-node results the harness already
    flushed to S3 are retained untouched (12.13)."""
    mark_run_failed(inp['test_run_id'], TIMEOUT_MESSAGE, timeout=True)
    return {'ok': True, 'status': 'failed', 'timeout': True}


def summarize_ecs_task_failure(cause: Any) -> Optional[str]:
    """Concise human-readable summary of an ECS RunTask failure Cause.

    When the Fargate sandbox task fails, Step Functions delivers the whole
    ECS task description (Attachments, network interfaces, container
    states, ...) as the Cause JSON string - unreadable in the portal's
    failure banner. Extract just what a user needs, in preference order:

        1. Containers[].Reason (e.g. image pull / OOM failures), with the
           exit code appended when known,
        2. the container exit code ("The sandbox container exited with
           code N"),
        3. the task-level StoppedReason, then StopCode.

    Returns None when ``cause`` is not JSON or not an ECS task shape, so
    the caller keeps its plain-text fallback.
    """
    try:
        task = json.loads(cause)
    except (TypeError, ValueError):
        return None
    if not isinstance(task, dict):
        return None

    containers = [c for c in (task.get('Containers') or [])
                  if isinstance(c, dict)]
    is_ecs_task = bool(containers) or any(
        key in task for key in ('StoppedReason', 'StopCode', 'TaskArn'))
    if not is_ecs_task:
        return None

    exit_code = next((c.get('ExitCode') for c in containers
                      if isinstance(c.get('ExitCode'), int)
                      and not isinstance(c.get('ExitCode'), bool)), None)
    reason = next((str(c['Reason']) for c in containers if c.get('Reason')),
                  None)

    if reason:
        if exit_code is not None:
            return '{0} (exit code {1})'.format(reason, exit_code)
        return reason
    if exit_code is not None:
        return 'The sandbox container exited with code {0}'.format(exit_code)
    stopped = task.get('StoppedReason') or task.get('StopCode')
    if stopped:
        return str(stopped)
    return 'The sandbox task failed'


def step_record_failure(inp: Dict) -> Dict:
    """The sandbox task (or an internal step) failed: mark the run failed.
    Partial results already flushed to S3 are retained (12.10)."""
    error_info = inp.get('errorInfo') or {}
    cause = error_info.get('Cause')
    # For ECS task failures the Cause is the raw task JSON blob; extract a
    # concise, readable message instead of storing the blob.
    message = summarize_ecs_task_failure(cause) if cause else None
    if message is None:
        message = cause or error_info.get('Error') \
            or 'Test run execution failed'
        # Step Functions delivers Cause as a JSON string for service
        # errors; keep the failure record readable.
        if isinstance(message, str) and len(message) > 512:
            message = message[:512]
    mark_run_failed(inp['test_run_id'], str(message))
    return {'ok': True, 'status': 'failed', 'timeout': False}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

STEPS = {
    'validate': step_validate,
    'compile': step_compile,
    'collect': step_collect,
    'record_timeout': step_record_timeout,
    'record_failure': step_record_failure,
}


def handler(event: Dict, context: Any) -> Dict:
    """Dispatch on event['step'] with event['input'] as the state input"""
    step = event.get('step')
    inp = event.get('input') or {}
    logger.info('Test run step %s for run %s', step, inp.get('test_run_id'))
    step_fn = STEPS.get(step)
    if step_fn is None:
        raise ValueError('Unknown test run step: {0!r}'.format(step))
    return step_fn(inp)
