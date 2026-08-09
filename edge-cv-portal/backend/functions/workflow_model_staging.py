"""
Triton model staging for workflow test runs (Workflow Manager).

Cloud test runs execute the compiled pipeline in the Fargate sandbox,
whose Triton model repository (/aws_dda/dda_triton/triton_model_repo)
starts empty. For every model_inference node in the workflow under
test, this module resolves the node's ``modelName`` against the portal
model registry (MODELS_TABLE, scoped to the Use_Case), picks a
CPU-runnable Greengrass component variant, locates the component's S3
model artifact from its recipe, and copies the artifact zip into the
portal artifacts bucket under the test run's prefix
(``.../test-runs/{test_run_id}/models/{modelName}.zip``). The resulting
staging manifest ``[{nodeId, modelName, s3Key}, ...]`` travels through
the state-machine input into the sandbox container (STAGED_MODELS env),
where the harness unpacks each artifact into the Triton model
repository before launching the pipeline.

Artifact layout (inspected against a live registry, documented for the
harness-side conversion):

- Registry items (MODELS_TABLE) carry ``name``, ``version``,
  ``usecase_id`` and ``component_arns`` keyed by device type, e.g.::

      component_arns: {
        "jetson-xavier-jp5": "arn:aws:greengrass:...:components:
                              model-cookies-binary-jetson-xavier-jp5:versions:3.0.0",
        "x86_64-cpu":        "arn:aws:greengrass:...:components:
                              model-cookies-binary-x86-64-cpu:versions:3.0.0"
      }

- The component recipe (greengrass:GetComponent) contains one artifact::

      Manifests[0].Artifacts[0].Uri =
          s3://<use-case bucket>/model_artifacts/model-<hash>/
              <hash>_greengrass_model_component.zip

  and a Startup lifecycle that runs ``model_convertor.py`` on-device.

- The zip is NOT a ready Triton model repository: it contains the raw
  runtime artifact plus its manifest (e.g. ``model.onnx`` +
  ``manifest.json`` with ``runtime: "onnx"``). The device-side
  ``src/backend/dda_triton/model_convertor.py`` converts it into a
  three-entry python-backend Triton repository (``base_<name>``,
  ``marshal_<name>``, ensemble ``<name>``); the sandbox harness
  replicates that conversion (test-sandbox/harness/model_staging.py).

Only CPU-executable variants can run in the sandbox (no GPU on
Fargate; the CPU Triton binary lives at /opt/tritonserver). Variant
selection therefore prefers component names ending ``-x86-64-cpu``,
then ``-onnx``; a model with neither fails the run with a clear
per-node error (Requirement 12.10 semantics) before any execution
starts.

IAM: the WorkflowTesting Lambda role (compute-stack createLambdaRole)
already carries greengrass:GetComponent on ``components:*``, read
access to MODELS_TABLE, and s3:GetObject on the portal-managed data
buckets, so no additional grant is needed for the copy. Cross-account
Use_Cases go through the same shared_utils assume-role path
(get_usecase_client) every other portal Lambda uses for use-case data.
The sandbox task role stays untouched: it only ever reads the staged
copy from the portal artifacts bucket (Requirement 12.9).
"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

logger = logging.getLogger()

#: Node type whose modelName parameter references the model registry.
MODEL_INFERENCE_TYPE = 'model_inference'

#: CPU-runnable component-name suffixes, in preference order: the
#: x86_64 CPU build first, then the architecture-neutral ONNX package
#: (the sandbox runs the ONNX runtime on CPU).
CPU_VARIANT_SUFFIXES = ('-x86-64-cpu', '-onnx')

#: Error codes recorded on per-node error records.
CODE_MODEL_NOT_REGISTERED = 'MODEL_NOT_REGISTERED'
CODE_NO_CPU_VARIANT = 'MODEL_NO_CPU_VARIANT'
CODE_MODEL_STAGING_FAILED = 'MODEL_STAGING_FAILED'


def no_cpu_variant_message(model_name: str) -> str:
    """The per-node error for a model without a CPU-runnable variant."""
    return ('Model {0} has no CPU-compatible (x86_64/ONNX) variant for '
            'cloud testing'.format(model_name))


def model_inference_nodes(definition: Any) -> List[Dict[str, Optional[str]]]:
    """The model_inference nodes of a raw Workflow_Definition document.

    Returns ``[{nodeId, modelName}, ...]`` in document order. Parses the
    stored JSON shape directly (nodes[].type / nodes[].parameters) so no
    workflow_core import is needed; a missing/blank modelName yields
    ``None`` (the validator would have failed the run anyway, but the
    caller degrades to a per-node error rather than crashing).
    """
    if not isinstance(definition, dict):
        return []
    nodes = definition.get('nodes')
    if not isinstance(nodes, list):
        return []
    found: List[Dict[str, Optional[str]]] = []
    for node in nodes:
        if not isinstance(node, dict) or node.get('type') != MODEL_INFERENCE_TYPE:
            continue
        parameters = node.get('parameters')
        model_name = None
        if isinstance(parameters, dict):
            value = parameters.get('modelName')
            if isinstance(value, str) and value.strip():
                model_name = value
        found.append({'nodeId': node.get('id'), 'modelName': model_name})
    return found


def component_name_from_arn(arn: Any) -> Optional[str]:
    """The component name of a Greengrass component-version ARN
    (``arn:aws:greengrass:<region>:<acct>:components:<name>:versions:<v>``)."""
    if not isinstance(arn, str):
        return None
    parts = arn.split(':')
    if len(parts) >= 7 and parts[5] == 'components':
        return parts[6] or None
    return None


def select_cpu_component_arn(component_arns: Any) -> Optional[str]:
    """The ARN of the best CPU-runnable component variant, or None.

    Preference: component names ending ``-x86-64-cpu`` first, then
    ``-onnx`` (CPU_VARIANT_SUFFIXES). GPU/Jetson-compiled variants
    (e.g. ``-jetson-xavier-jp5``) can never run on the Fargate CPU
    sandbox and are ignored.
    """
    if not isinstance(component_arns, dict):
        return None
    for suffix in CPU_VARIANT_SUFFIXES:
        for arn in component_arns.values():
            name = component_name_from_arn(arn)
            if name and name.endswith(suffix):
                return arn
    return None


def registry_name_candidates(model_name: str) -> List[str]:
    """Registry ``name`` values that may denote ``model_name``.

    The Workflow_Builder model dropdown stores the training-job
    ``model_name`` (e.g. ``yolo_test``), while the packaging pipeline
    registers the model in MODELS_TABLE under its component base name:
    ``model-`` prefix with underscores normalized to hyphens (e.g.
    ``model-yolo-test``). Both spellings resolve so workflows built from
    either source stage correctly.
    """
    candidates = [model_name]
    normalized = model_name.replace('_', '-')
    if normalized not in candidates:
        candidates.append(normalized)
    for base in (model_name, normalized):
        prefixed = 'model-' + base
        if not base.startswith('model-') and prefixed not in candidates:
            candidates.append(prefixed)
    return candidates


def resolve_model_item(model_items: List[Dict], model_name: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Resolve the registry item to stage for ``model_name``.

    ``model_items`` are the Use_Case's MODELS_TABLE items. Among items
    whose ``name`` matches (exactly, or via the component base-name
    convention - see registry_name_candidates), the newest (created_at)
    one carrying a CPU-runnable variant wins. Returns
    ``(item, cpu_component_arn)``; ``(None, None)`` when the name is not
    registered at all, and ``(item, None)`` when it is registered but no
    variant can run on CPU.
    """
    matching: List[Dict] = []
    for candidate in registry_name_candidates(model_name):
        matching = [item for item in model_items
                    if isinstance(item, dict) and item.get('name') == candidate]
        if matching:
            break
    if not matching:
        return None, None
    matching.sort(key=lambda item: item.get('created_at') or 0, reverse=True)
    for item in matching:
        arn = select_cpu_component_arn(item.get('component_arns'))
        if arn:
            return item, arn
    return matching[0], None


def artifact_location_from_recipe(recipe: Any) -> Optional[Tuple[str, str]]:
    """The (bucket, key) of the component's zip artifact.

    Recipes list the model zip as ``Manifests[].Artifacts[].Uri`` in
    ``s3://bucket/key`` form (see the module docstring for the observed
    layout). The first ``.zip`` S3 artifact wins; None when the recipe
    carries no such artifact.
    """
    if not isinstance(recipe, dict):
        return None
    fallback: Optional[Tuple[str, str]] = None
    for manifest in recipe.get('Manifests') or []:
        if not isinstance(manifest, dict):
            continue
        for artifact in manifest.get('Artifacts') or []:
            if not isinstance(artifact, dict):
                continue
            uri = artifact.get('Uri')
            if not isinstance(uri, str) or not uri.startswith('s3://'):
                continue
            bucket, _, key = uri[len('s3://'):].partition('/')
            if not bucket or not key:
                continue
            if key.lower().endswith('.zip'):
                return bucket, key
            fallback = fallback or (bucket, key)
    return fallback


def load_component_recipe(greengrass_client, component_arn: str) -> Dict:
    """Fetch and parse a component recipe (JSON) via greengrass:GetComponent."""
    response = greengrass_client.get_component(arn=component_arn,
                                               recipeOutputFormat='JSON')
    recipe = response.get('recipe')
    if hasattr(recipe, 'read'):
        recipe = recipe.read()
    if isinstance(recipe, (bytes, bytearray)):
        recipe = recipe.decode('utf-8')
    return json.loads(recipe)


def staged_model_key(results_s3_key: str, model_name: str) -> str:
    """S3 key of a staged model zip: ``models/{modelName}.zip`` under the
    test run's prefix (next to results.json / compiled_pipeline.json)."""
    prefix = results_s3_key.rsplit('/', 1)[0] if '/' in results_s3_key else ''
    return '{0}models/{1}.zip'.format(prefix + '/' if prefix else '', model_name)


def stage_models_for_run(
    nodes: List[Dict],
    model_items: List[Dict],
    greengrass_client,
    source_s3,
    portal_s3,
    artifacts_bucket: str,
    results_s3_key: str,
) -> Tuple[List[Dict], List[Dict]]:
    """Stage the model artifact of every model_inference node.

    Returns ``(staged, errors)``:

    - ``staged``: the staging manifest ``[{nodeId, modelName, s3Key}]``
      recorded in the state-machine input (one entry per node; nodes
      sharing a model share the same staged object).
    - ``errors``: per-node error records ``{nodeId, modelName, status,
      outputs, stubActivity, error:{code, message}}`` (the
      results-document shape of workflow_test_steps.error_records, plus
      the model name). Staging is best-effort (12.16, 12.17): the caller
      converts these records into ``STAGING_FALLBACKS`` entries — the
      node is omitted from ``STAGED_MODELS``, the run still starts, and
      the sandbox runs the node with the simulated inference outcome.
    """
    staged: List[Dict] = []
    errors: List[Dict] = []
    copied_keys: Dict[str, str] = {}  # modelName -> staged s3 key

    def record_error(node_id, model_name, code, message):
        errors.append({
            'nodeId': node_id,
            'modelName': model_name,
            'status': 'error',
            'outputs': [],
            'stubActivity': [],
            'error': {'code': code, 'message': message},
        })

    for node in nodes:
        node_id = node.get('nodeId')
        model_name = node.get('modelName')
        if not model_name:
            record_error(node_id, model_name, CODE_MODEL_NOT_REGISTERED,
                         'Model inference node has no model selected')
            continue

        if model_name in copied_keys:
            staged.append({'nodeId': node_id, 'modelName': model_name,
                           's3Key': copied_keys[model_name]})
            continue

        item, component_arn = resolve_model_item(model_items, model_name)
        if item is None:
            record_error(node_id, model_name, CODE_MODEL_NOT_REGISTERED,
                         'Model {0} is not registered for this use '
                         'case'.format(model_name))
            continue
        if not component_arn:
            record_error(node_id, model_name, CODE_NO_CPU_VARIANT,
                         no_cpu_variant_message(model_name))
            continue

        try:
            recipe = load_component_recipe(greengrass_client, component_arn)
        except (ClientError, ValueError, KeyError) as error:
            logger.error('Could not load recipe for %s: %s',
                         component_arn, str(error))
            record_error(node_id, model_name, CODE_MODEL_STAGING_FAILED,
                         'Model {0}: the component recipe could not be '
                         'read ({1})'.format(
                             model_name,
                             component_name_from_arn(component_arn)))
            continue

        location = artifact_location_from_recipe(recipe)
        if not location:
            record_error(node_id, model_name, CODE_MODEL_STAGING_FAILED,
                         'Model {0}: the component recipe declares no S3 '
                         'model artifact'.format(model_name))
            continue

        source_bucket, source_key = location
        destination_key = staged_model_key(results_s3_key, model_name)
        try:
            # Stream the artifact through the Lambda: the source is a
            # use-case data bucket (possibly cross-account, read via the
            # per-usecase client), the destination the portal artifacts
            # bucket the sandbox task role can read (12.9).
            body = source_s3.get_object(Bucket=source_bucket,
                                        Key=source_key)['Body']
            portal_s3.upload_fileobj(body, artifacts_bucket, destination_key)
        except ClientError as error:
            logger.error('Could not stage model artifact s3://%s/%s: %s',
                         source_bucket, source_key, str(error))
            record_error(node_id, model_name, CODE_MODEL_STAGING_FAILED,
                         'Model {0}: the model artifact could not be '
                         'copied from s3://{1}/{2}'.format(
                             model_name, source_bucket, source_key))
            continue

        copied_keys[model_name] = destination_key
        staged.append({'nodeId': node_id, 'modelName': model_name,
                       's3Key': destination_key})
        logger.info('Staged model %s (component %s) at s3://%s/%s',
                    model_name, component_name_from_arn(component_arn),
                    artifacts_bucket, destination_key)

    return staged, errors
