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

                     Compiles against the merged Node_Type_Catalog of the
                     run's Use_Case (custom-node-designer 12.1): registered
                     Custom_Node_Types resolve with the versions pinned at
                     workflow save (custom_node_type_pins in the execution
                     input). Custom_Node_Types whose backing Plugin_Record
                     has a successful x86_64 Plugin_Artifact get that
                     artifact staged under the run's prefix
                     (.../test-runs/{id}/plugins/) for the sandbox task to
                     download into its plugin scan path; Custom_Node_Types
                     lacking one are substituted with a pass-through
                     recording stub (identity element named
                     custom_stub_<nodeId>, in addition to the
                     hardware-dependent stubbing rules) that the harness
                     identifies as stubbed in the test run report
                     (custom-node-designer 12.2). The staged plugins and
                     stubbed type ids are written to the
                     custom_plugins.json manifest next to the compiled
                     document.
    collect          Read the per-node results the sandbox flushed
                     incrementally to S3 and mark the run completed, or
                     failed with the failing node identified (12.10).
    record_timeout   The 10-minute execution timeout stopped the sandbox
                     task: mark the run failed with a timeout indication;
                     partial per-node results already in S3 are retained
                     untouched (12.13).
    record_failure   The sandbox task (or an internal step) failed: mark the
                     run failed; partial results are retained (12.10).

This module is standalone (workflow_core layer + node_catalog_resolution
only, no shared_utils) so the test-runner stack can deploy it with a
minimal role: TestRuns table read/write, portal-artifacts S3 read/write,
and read-only access to the node-designer CustomNodeTypes/PluginRecords
tables for the merged-catalog resolution (degrading to the built-in
catalog when the node-designer stack is not deployed). It has no
Greengrass or device permissions (12.9).
"""
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence

import boto3
from botocore.exceptions import ClientError

# workflow_core ships as a Lambda layer under /opt/python
sys.path.append('/opt/python')
from workflow_core.catalog.custom import resolve_catalog
from workflow_core.catalog.models import ARCH_SIM, GstMapping, NodeTypeDescriptor
from workflow_core.compiler import CompileContext, compile as compile_workflow
from workflow_core.serializer import parse as parse_definition
from workflow_core.validator import SEVERITY_ERROR, validate as run_validator

from model_registry_snapshot import build_model_registry_snapshot

# Merged-catalog helpers shared with the other catalog consumers (task 9.2);
# bundled in the same functions asset the test-runner stack deploys. Imports
# workflow_core + boto3 only, keeping this handler shared_utils-free.
from node_catalog_resolution import (
    descriptors_from_items,
    load_registered_node_types,
    resolution_items,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

TEST_RUNS_TABLE = os.environ.get('TEST_RUNS_TABLE')
#: PluginRecords table of the node-designer stack; unset when that stack
#: is not deployed (every custom node then counts as artifact-less, which
#: cannot be reached in practice because the catalog merge also degrades
#: and validation rejects the unknown type first).
PLUGIN_RECORDS_TABLE = os.environ.get('PLUGIN_RECORDS_TABLE')

#: Model_Registry sources for model-reference resolution in step_validate
#: (same snapshot the Validate endpoint uses — workflow_validation.py).
#: Unset TRAINING_JOBS_TABLE skips the resolution checks (pre-feature
#: behavior), matching the endpoint's degradation.
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
MODELS_TABLE = os.environ.get('MODELS_TABLE')

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


def custom_plugins_progress_message(count: int) -> str:
    """Progress entry recorded when custom x86_64 Plugin_Artifacts are
    staged for the sandbox task (custom-node-designer 12.1)."""
    return ('Staged {0} custom plugin artifact(s) for the sandbox'
            .format(count))


def custom_stub_progress_message(stubbed_type_ids: List[str]) -> str:
    """Progress entry recorded when Custom_Node_Types without an x86_64
    build are substituted with pass-through stubs (12.2)."""
    return ('Substituting pass-through stubs for custom node type(s) '
            'without an x86_64 build: {0}'.format(', '.join(stubbed_type_ids)))


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def run_prefix(results_s3_key: str) -> str:
    """The run's S3 prefix (everything up to the results document name)."""
    prefix = results_s3_key.rsplit('/', 1)[0] if '/' in results_s3_key else ''
    return prefix + '/' if prefix else ''


def compiled_document_key(results_s3_key: str) -> str:
    """The Compiled Pipeline Document lives next to the results document:
    .../test-runs/{test_run_id}/compiled_pipeline.json"""
    return run_prefix(results_s3_key) + 'compiled_pipeline.json'


# ---------------------------------------------------------------------------
# Custom_Node_Type support (custom-node-designer Requirements 12.1, 12.2)
#
# The "pure decision logic" section is pure over plain dicts/descriptors so
# task 13.2 can property-test that stubbing is exactly the unavailable
# custom nodes without AWS. Loaders and staging live below it.
# ---------------------------------------------------------------------------

#: Identity-element name prefix of the custom-node pass-through recording
#: stub; the sandbox harness identifies stubbed nodes by it (12.2).
CUSTOM_STUB_ELEMENT_PREFIX = 'custom_stub_'

#: Manifest written next to the Compiled Pipeline Document listing the
#: staged custom x86_64 Plugin_Artifacts the sandbox task downloads into
#: its plugin scan path, plus the stubbed Custom_Node_Type ids (12.1, 12.2).
CUSTOM_PLUGINS_MANIFEST_NAME = 'custom_plugins.json'

#: buildStatus value of a usable per-arch Plugin_Record artifact entry.
BUILD_SUCCEEDED = 'succeeded'


# ------------------------------------------------------ pure decision logic

def x86_64_artifact_available(entry: Optional[Dict]) -> bool:
    """Whether a Plugin_Record's x86_64 artifact entry is a successfully
    built Plugin_Artifact the sandbox can execute (12.1)."""
    return bool(entry
                and entry.get('buildStatus') == BUILD_SUCCEEDED
                and entry.get('s3Key'))


def stubbed_custom_type_ids(custom_type_ids: Iterable[str],
                            artifact_entries: Dict[str, Optional[Dict]]
                            ) -> FrozenSet[str]:
    """The Custom_Node_Types that get the pass-through recording stub:
    exactly those lacking a successful x86_64 Plugin_Artifact (12.2) —
    everything else executes its real x86_64 build (12.1)."""
    return frozenset(
        type_id for type_id in custom_type_ids
        if not x86_64_artifact_available(artifact_entries.get(type_id))
    )


def custom_stub_mapping(arch: str) -> GstMapping:
    """The pass-through recording stub mapping: an identity element named
    custom_stub_<nodeId> (the compiler resolves {custom_stub_name} per
    node) that passes input frames through unchanged; the harness records
    the substitution as stub activity in the test run report (12.2)."""
    return GstMapping(
        arch=arch,
        element_chain=[{
            'factory': 'identity',
            'args_template': {'name': '{custom_stub_name}'},
        }],
        plugin_dependencies=[],
    )


def stub_descriptor(descriptor: NodeTypeDescriptor,
                    target_arch: str) -> NodeTypeDescriptor:
    """The descriptor of a stubbed Custom_Node_Type: identical declaration
    but every realization replaced by the pass-through recording stub.
    Both the target architecture and the ``sim`` architecture are mapped,
    so hardware-dependent custom types (which the simulation compiler
    resolves via the existing sim-stub rule) stub identically (12.2:
    "in addition to the hardware-dependent stubbing rules")."""
    archs = [target_arch] if target_arch == ARCH_SIM else [target_arch, ARCH_SIM]
    return NodeTypeDescriptor(
        type_id=descriptor.type_id,
        category=descriptor.category,
        display_name=descriptor.display_name,
        inputs=descriptor.inputs,
        outputs=descriptor.outputs,
        parameters=descriptor.parameters,
        mappings=[custom_stub_mapping(arch) for arch in archs],
        hardware_dependent=descriptor.hardware_dependent,
    )


def apply_custom_stubs(custom_descriptors: Sequence[NodeTypeDescriptor],
                       stub_type_ids: FrozenSet[str],
                       target_arch: str) -> List[NodeTypeDescriptor]:
    """Replace exactly the stubbed descriptors with their pass-through
    recording stubs; every other descriptor is untouched (12.2)."""
    return [
        stub_descriptor(descriptor, target_arch)
        if descriptor.type_id in stub_type_ids else descriptor
        for descriptor in custom_descriptors
    ]


def plugin_file_name(artifact_s3_key: str) -> str:
    """The staged .so file name of a Plugin_Library artifact key."""
    name = artifact_s3_key.rsplit('/', 1)[-1] or 'plugin.so'
    return name if name.endswith('.so') else name + '.so'


def staged_plugin_key(results_s3_key: str, file_name: str) -> str:
    """Run-prefix key a custom x86_64 Plugin_Artifact is staged to for
    the sandbox task: .../test-runs/{id}/plugins/{plugin}.so (12.1)."""
    return run_prefix(results_s3_key) + 'plugins/' + file_name


def custom_plugins_manifest_key(results_s3_key: str) -> str:
    """.../test-runs/{test_run_id}/custom_plugins.json"""
    return run_prefix(results_s3_key) + CUSTOM_PLUGINS_MANIFEST_NAME


# ------------------------------------------------------------------ loaders

def load_custom_catalog_items(inp: Dict) -> List[Dict]:
    """The resolved CustomNodeTypes version items of the run's Use_Case,
    honoring the Custom_Node_Type versions pinned at workflow save
    (custom_node_type_pins from the execution input, 14.2). Returns []
    when the run has no Use_Case or the node-designer stack is absent,
    so the built-in catalog is used unchanged."""
    usecase_id = inp.get('usecase_id')
    if not usecase_id:
        return []
    items = load_registered_node_types(usecase_id)
    if not items:
        return []
    return resolution_items(items, inp.get('custom_node_type_pins') or {})


def merged_catalog(custom_descriptors: Sequence[NodeTypeDescriptor]) -> tuple:
    """The built-in catalog merged with the resolved custom descriptors
    (built-ins win on type-id collision)."""
    return resolve_catalog(custom_descriptors)


def load_x86_64_artifact_entries(items_by_type: Dict[str, Dict],
                                 type_ids: Iterable[str]
                                 ) -> Dict[str, Optional[Dict]]:
    """The x86_64 artifact entry of each used Custom_Node_Type's pinned
    backing Plugin_Record version, or None when the record/entry/table is
    missing (fails closed: the type is then stubbed, 12.2)."""
    entries: Dict[str, Optional[Dict]] = {}
    table = dynamodb.Table(PLUGIN_RECORDS_TABLE) if PLUGIN_RECORDS_TABLE else None
    for type_id in type_ids:
        entry = None
        item = items_by_type.get(type_id)
        if (table is not None and item
                and item.get('plugin_id') is not None
                and item.get('plugin_version') is not None):
            try:
                response = table.get_item(Key={
                    'plugin_id': item['plugin_id'],
                    'version': int(item['plugin_version']),
                })
                record = response.get('Item')
            except ClientError as e:
                logger.warning('Could not load plugin record %s v%s: %s',
                               item.get('plugin_id'),
                               item.get('plugin_version'), str(e))
                record = None
            if record:
                artifacts = record.get('artifacts') or {}
                candidate = artifacts.get('x86_64')
                entry = candidate if isinstance(candidate, dict) else None
        entries[type_id] = entry
    return entries


def stage_custom_plugins(bucket: str, results_s3_key: str,
                         real_type_ids: Iterable[str],
                         artifact_entries: Dict[str, Optional[Dict]]
                         ) -> List[Dict]:
    """Copy each executable custom x86_64 Plugin_Artifact from the
    Plugin_Library into the run's plugins/ prefix so the sandbox task
    (whose access is the portal artifacts bucket) downloads it into its
    plugin scan path (12.1). Returns the manifest plugin entries."""
    staged: List[Dict] = []
    for type_id in sorted(real_type_ids):
        entry = artifact_entries[type_id]
        source_key = str(entry['s3Key'])
        file_name = plugin_file_name(source_key)
        target_key = staged_plugin_key(results_s3_key, file_name)
        s3.copy_object(
            Bucket=bucket,
            CopySource={'Bucket': bucket, 'Key': source_key},
            Key=target_key,
        )
        staged.append({
            'nodeTypeId': type_id,
            'fileName': file_name,
            's3Key': target_key,
        })
    return staged


def write_custom_plugins_manifest(bucket: str, results_s3_key: str,
                                  plugins: List[Dict],
                                  stubbed_type_ids: Iterable[str]) -> str:
    """Write the custom_plugins.json manifest next to the compiled
    document; the sandbox harness stages the listed plugins and the
    report identifies the stubbed Custom_Node_Types (12.1, 12.2)."""
    key = custom_plugins_manifest_key(results_s3_key)
    document = {
        'plugins': plugins,
        'stubbedNodeTypeIds': sorted(stubbed_type_ids),
    }
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(document, indent=2).encode('utf-8'),
        ContentType='application/json',
    )
    return key


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
    """Validate: parse the definition and run all validator checks against
    the merged catalog of the run's Use_Case (12.4; custom-node-designer
    12.1 — custom nodes are known types here exactly as in
    workflow_validation.py). Errors are recorded with node/connection ids
    and fail the run (12.12)."""
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

    # Model-reference resolution (vllm-triton-inference 6.5, 6.12): the
    # same Model_Registry snapshot the Validate endpoint loads, so a
    # test run can never pass a workflow whose model references the
    # Validate button rejects. Fail closed: a registry read error fails
    # the run rather than recording a validation pass that skipped the
    # resolution check.
    model_registry = None
    if TRAINING_JOBS_TABLE:
        try:
            model_registry = build_model_registry_snapshot(
                inp['usecase_id'], TRAINING_JOBS_TABLE, MODELS_TABLE,
                dynamodb)
        except ClientError as e:
            logger.error('Model registry snapshot unavailable for usecase '
                         '%s: %s', inp.get('usecase_id'), str(e))
            errors = [{'code': 'MODEL_REGISTRY_LOAD_FAILED',
                       'message': 'Model registry could not be loaded for '
                                  'model-reference validation',
                       'nodeId': None, 'connectionId': None}]
            record_short_circuit(
                inp, errors,
                'Model registry could not be loaded; the pipeline was not '
                'executed')
            return {'ok': False, 'stage': 'validate', 'errors': errors}
    else:
        logger.warning('TRAINING_JOBS_TABLE not configured; model-reference '
                       'resolution is skipped for this test run')

    catalog = merged_catalog(
        descriptors_from_items(load_custom_catalog_items(inp)))
    findings = run_validator(result.graph, catalog,
                             model_registry=model_registry)
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
    against the merged catalog of the run's Use_Case (12.4, 12.6;
    custom-node-designer 12.1) and stage the Compiled Pipeline Document
    for the sandbox. Custom_Node_Types with a successful x86_64
    Plugin_Artifact get the artifact staged under the run's prefix; those
    without one are substituted with the pass-through recording stub
    (12.2). Compile errors short-circuit the run (12.12)."""
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

    # Merged catalog with the pinned Custom_Node_Type versions (12.1).
    custom_items = load_custom_catalog_items(inp)
    custom_descriptors = descriptors_from_items(custom_items)
    items_by_type = {
        item['node_type_id']: item
        for item in custom_items if item.get('node_type_id')
    }

    # The stub-vs-real decision per used custom node keys purely on the
    # presence of a successful x86_64 Plugin_Artifact (12.1, 12.2).
    custom_type_ids = {d.type_id for d in custom_descriptors}
    used_custom_ids = sorted(
        {node.type for node in result.graph.nodes} & custom_type_ids)
    artifact_entries = load_x86_64_artifact_entries(items_by_type,
                                                    used_custom_ids)
    stub_ids = stubbed_custom_type_ids(used_custom_ids, artifact_entries)

    catalog = merged_catalog(
        apply_custom_stubs(custom_descriptors, stub_ids, target_arch))

    context = CompileContext(
        workflow_id=str(inp.get('workflow_id') or ''),
        workflow_version=str(inp.get('workflow_version') or ''),
    )
    outcome = compile_workflow(
        result.graph,
        target_arch,
        context=context,
        simulation=simulation,
        catalog=catalog,
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

    # Custom plugin staging + manifest (only when custom nodes are used,
    # keeping runs without them byte-identical to the pre-existing flow).
    stubbed = sorted(stub_ids)
    if used_custom_ids:
        real_ids = [t for t in used_custom_ids if t not in stub_ids]
        plugins = stage_custom_plugins(bucket, inp['results_s3_key'],
                                       real_ids, artifact_entries)
        write_custom_plugins_manifest(bucket, inp['results_s3_key'],
                                      plugins, stubbed)
        progress = []
        if plugins:
            progress.append(custom_plugins_progress_message(len(plugins)))
        if stubbed:
            progress.append(custom_stub_progress_message(stubbed))
        if progress:
            append_run_progress(inp['test_run_id'], *progress)

    # The next state is the Fargate sandbox task (no Lambda runs there),
    # so the sandbox-start entry is appended here on compile success.
    append_run_progress(inp['test_run_id'],
                        PROGRESS_COMPILE_SUCCEEDED, PROGRESS_STARTING_SANDBOX)
    return {'ok': True, 'compiled_s3_key': key,
            'stubbed_custom_node_types': stubbed}


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
