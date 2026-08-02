"""Model_Registry snapshot for model-reference validation.

Shared by the Validate endpoint (workflow_validation.py) and the test
run's Validate step (workflow_test_steps.py) so both validation paths
resolve model_ref parameters against the same registry view — the
divergence where a test run silently skipped model-reference checks the
Validate button reported is what this module removes
(vllm-triton-inference Requirements 6.5, 6.12).

The snapshot maps model name -> registry record (each record a mapping
whose ``model_type`` key discriminates ``vllm`` records from vision
records), built from:

- the training-jobs table (``usecase-training-index``): the source of
  truth for model records — trained models, imported BYOM models, and
  vLLM_Model_Records (written by model_import.py with
  ``model_type: 'vllm'``) — keyed by ``model_name``, the value the
  Workflow_Designer model dropdown stores;
- the models table (``usecase-models-index``): published models
  registered under their component base name (e.g. ``model-yolo-test``),
  so workflows referencing either spelling resolve (the
  workflow_model_staging convention). The ``model_type`` of these items
  is joined from the backing training-job record via ``training_job_id``.

When several records share a name the newest (``created_at``) wins.
DynamoDB errors propagate to the caller — both callers fail closed
rather than recording a validation pass that skipped the resolution
check.
"""

import decimal
from typing import Any, Dict, List, Optional


def decimal_to_native(obj: Any) -> Any:
    """DynamoDB Decimal values converted to native int/float, recursively."""
    if isinstance(obj, list):
        return [decimal_to_native(item) for item in obj]
    if isinstance(obj, dict):
        return {key: decimal_to_native(value) for key, value in obj.items()}
    if isinstance(obj, decimal.Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    return obj


def _query_all(table, **kwargs) -> List[Dict]:
    """Every page of a DynamoDB query."""
    items: List[Dict] = []
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def build_model_registry_snapshot(usecase_id: str,
                                  training_jobs_table: str,
                                  models_table: Optional[str],
                                  dynamodb) -> Dict[str, Dict]:
    """The Use_Case's Model_Registry snapshot (see module docstring).

    :param training_jobs_table: training-jobs table name (required).
    :param models_table: models table name, or None to skip the
        published-name alias pass.
    :param dynamodb: boto3 DynamoDB service resource.
    """
    training_items = _query_all(
        dynamodb.Table(training_jobs_table),
        IndexName='usecase-training-index',
        KeyConditionExpression='usecase_id = :uid',
        ExpressionAttributeValues={':uid': usecase_id},
    )

    snapshot: Dict[str, Dict] = {}
    by_training_id: Dict[str, Dict] = {}
    for item in training_items:
        item = decimal_to_native(item)
        training_id = item.get('training_id')
        if isinstance(training_id, str):
            by_training_id[training_id] = item
        name = item.get('model_name')
        if not isinstance(name, str) or not name:
            continue
        existing = snapshot.get(name)
        if existing is None or (item.get('created_at') or 0) >= \
                (existing.get('created_at') or 0):
            snapshot[name] = item

    if models_table:
        model_items = _query_all(
            dynamodb.Table(models_table),
            IndexName='usecase-models-index',
            KeyConditionExpression='usecase_id = :uid',
            ExpressionAttributeValues={':uid': usecase_id},
        )
        for item in model_items:
            item = decimal_to_native(item)
            name = item.get('name')
            if not isinstance(name, str) or not name or name in snapshot:
                # Training-job records win their exact name; published
                # component base names only add alias spellings.
                continue
            if 'model_type' not in item:
                backing = by_training_id.get(item.get('training_job_id'))
                if backing and isinstance(backing.get('model_type'), str):
                    item = dict(item)
                    item['model_type'] = backing['model_type']
            snapshot[name] = item

    return snapshot
