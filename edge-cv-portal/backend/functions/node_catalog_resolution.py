"""
Merged Node_Type_Catalog resolution for Custom_Node_Types
(custom-node-designer task 9.2).

Shared by the existing catalog consumers in this Lambda bundle:

    workflow_validation.py   GET /workflows/node-catalog serves the merged
                             palette catalog for a Use_Case (8.2, 8.3, 9.2,
                             9.6, 14.3); POST /workflows/{id}/validate
                             validates against the merged resolution
                             catalog (14.2, 14.3)
    workflow_generator.py    serializes the merged palette catalog into
                             the generation system prompt
    workflows.py             workflow save records the Custom_Node_Type
                             version used per custom node (14.2) via
                             referenced_node_type_versions

Two distinct merges exist:

- The **palette** merge (`resolve_palette_catalog`): what the Node_Palette
  may offer for *new placement*. Only the latest version of each
  Custom_Node_Type, only when the backing Plugin_Record version's
  Lifecycle_State is test or prod (dev excluded, Requirement 9.2), and
  never deprecated types (Requirement 14.3). Test-state entries carry a
  ``lifecycleState: "test"`` marker on the wire (Requirement 9.6).

- The **resolution** merge (`resolve_resolution_catalog`): what existing
  saved workflows resolve against for loading/validating/packaging.
  Deprecated types and every Lifecycle_State remain resolvable
  (Requirement 14.3: saved workflows stay loadable, packagable, and
  deployable), and versions pinned at workflow save are honored
  (Requirement 14.2), falling back to the latest registered version.

Everything in the "pure resolution logic" section is pure over plain
dicts (stored CustomNodeTypes items and wire declarations) so tasks
9.3/9.4 can property-test the merge/marker/exclusion logic without AWS.
The DynamoDB loaders live at the bottom, cleanly separated.

Reference-index note (task 9.2 decision): the removal scan in
custom_node_types.py prefers an inverted-index GSI (node-type-refs-index,
attribute ref_node_type_id) over WorkflowVersions. A DynamoDB GSI can
index only ONE scalar attribute value per item, but a workflow version
may reference SEVERAL Custom_Node_Types, so a scalar ref_node_type_id
cannot represent the reference set. The GSI is therefore NOT created;
workflow save records the ``custom_node_types`` map attribute
({typeId: typeVersion}) on every WorkflowVersions item instead, which
the existing scan fallback in custom_node_types.py already honors
without loading definition documents from S3.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from workflow_core.catalog.custom import (
    DeclarationError,
    descriptor_from_declaration,
    resolve_catalog,
)
from workflow_core.catalog.nodes import NODE_CATALOG

logger = logging.getLogger()

# Lifecycle_State values of the backing Plugin_Record version
# (plugin_records.py owns the state machine).
LIFECYCLE_DEV = 'dev'
LIFECYCLE_TEST = 'test'
LIFECYCLE_PROD = 'prod'

#: States whose Custom_Node_Types the palette may offer (Requirement 9.2:
#: dev excluded from the Node_Palette).
PALETTE_LIFECYCLE_STATES = frozenset({LIFECYCLE_TEST, LIFECYCLE_PROD})

#: Built-in type ids never count as custom references (resolve_catalog
#: lets built-ins win on collision).
BUILTIN_TYPE_IDS = frozenset(descriptor.type_id for descriptor in NODE_CATALOG)


# ------------------------------------------------------ pure resolution logic
#
# Pure over plain dicts: no AWS, no environment. Property tests (tasks
# 9.3/9.4) exercise these directly.

def latest_versions(node_type_items: Sequence[Dict]) -> Dict[str, Dict]:
    """Highest-version CustomNodeTypes item per node_type_id (14.1 retains
    every version; the palette and unpinned resolution serve the latest)."""
    latest: Dict[str, Dict] = {}
    for item in node_type_items:
        type_id = item.get('node_type_id')
        if not type_id:
            continue
        current = latest.get(type_id)
        if current is None or int(item.get('version', 0)) > int(current.get('version', 0)):
            latest[type_id] = item
    return latest


def lifecycle_marker(lifecycle_state: Optional[str]) -> Optional[str]:
    """Palette marker of one entry (Requirement 9.6): test-state entries
    carry the 'test' marker; prod entries carry none."""
    return LIFECYCLE_TEST if lifecycle_state == LIFECYCLE_TEST else None


def _backing_state(item: Dict, lifecycle_states: Dict[Tuple[str, int], str]
                   ) -> Optional[str]:
    """Lifecycle_State of the item's pinned backing Plugin_Record version;
    None when unknown (fails closed for the palette)."""
    plugin_id = item.get('plugin_id')
    plugin_version = item.get('plugin_version')
    if plugin_id is None or plugin_version is None:
        return None
    return lifecycle_states.get((plugin_id, int(plugin_version)))


def palette_entries(node_type_items: Sequence[Dict],
                    lifecycle_states: Dict[Tuple[str, int], str]
                    ) -> List[Tuple[Dict, Optional[str]]]:
    """The Custom_Node_Type version items the Node_Palette may offer,
    each paired with its lifecycle marker.

    Selection (deterministic, ordered by node_type_id):
      - the latest version of each type (14.1),
      - deprecated types excluded from new placement (14.3),
      - backing Plugin_Record Lifecycle_State test or prod only; dev or
        unknown states are excluded (9.2 — fail closed).
    """
    entries: List[Tuple[Dict, Optional[str]]] = []
    latest = latest_versions(node_type_items)
    for type_id in sorted(latest):
        item = latest[type_id]
        if item.get('deprecated'):
            continue
        state = _backing_state(item, lifecycle_states)
        if state not in PALETTE_LIFECYCLE_STATES:
            continue
        entries.append((item, lifecycle_marker(state)))
    return entries


def resolution_items(node_type_items: Sequence[Dict],
                     pinned_versions: Optional[Dict[str, Any]] = None
                     ) -> List[Dict]:
    """The Custom_Node_Type version items existing saved workflows
    resolve against for loading/validating/packaging.

    Versions pinned at workflow save are honored (14.2); types without a
    pin resolve to their latest version. Deprecated types and every
    Lifecycle_State stay resolvable (14.3). A pinned version that no
    longer exists falls back to the latest version of that type.
    """
    pinned = pinned_versions or {}
    by_version: Dict[Tuple[str, int], Dict] = {}
    for item in node_type_items:
        type_id = item.get('node_type_id')
        if not type_id:
            continue
        by_version[(type_id, int(item.get('version', 0)))] = item

    resolved: List[Dict] = []
    latest = latest_versions(node_type_items)
    for type_id in sorted(latest):
        item = latest[type_id]
        pin = pinned.get(type_id)
        if pin is not None:
            item = by_version.get((type_id, int(pin)), item)
        resolved.append(item)
    return resolved


def descriptors_from_items(items: Sequence[Dict]) -> List:
    """Frozen NodeTypeDescriptors from stored declaration JSON.

    Registration already validated every stored declaration through
    descriptor_from_declaration, so a conversion failure indicates a
    corrupted item; it is skipped with a log rather than failing the
    whole catalog.
    """
    descriptors = []
    for item in items:
        declaration = item.get('declaration')
        if not isinstance(declaration, dict):
            continue
        try:
            descriptors.append(descriptor_from_declaration(declaration))
        except DeclarationError as e:
            logger.warning(
                "Skipping stored custom node type %r v%s with an invalid "
                "declaration: %s",
                item.get('node_type_id'), item.get('version'), str(e))
    return descriptors


def resolve_palette_catalog(node_type_items: Sequence[Dict],
                            lifecycle_states: Dict[Tuple[str, int], str]
                            ) -> Tuple[tuple, Dict[str, str]]:
    """The merged palette catalog of one Use_Case (8.2, 9.2, 9.6, 14.3).

    Returns ``(catalog, markers)``: the built-in NODE_CATALOG merged with
    the eligible custom descriptors via resolve_catalog (built-ins win on
    type-id collision), plus ``{type_id: 'test'}`` for merged test-state
    entries (prod entries carry no marker).
    """
    entries = palette_entries(node_type_items, lifecycle_states)
    descriptors = []
    marker_by_type_id: Dict[str, str] = {}
    for item, marker in entries:
        converted = descriptors_from_items([item])
        if not converted:
            continue
        descriptor = converted[0]
        descriptors.append(descriptor)
        if marker:
            marker_by_type_id[descriptor.type_id] = marker
    merged = resolve_catalog(descriptors)
    merged_type_ids = {d.type_id for d in merged[len(NODE_CATALOG):]}
    markers = {type_id: marker
               for type_id, marker in marker_by_type_id.items()
               if type_id in merged_type_ids}
    return merged, markers


def resolve_resolution_catalog(node_type_items: Sequence[Dict],
                               pinned_versions: Optional[Dict[str, Any]] = None
                               ) -> tuple:
    """The merged catalog existing workflows load/validate/package
    against (14.2 pinning, 14.3 deprecated-but-resolvable)."""
    items = resolution_items(node_type_items, pinned_versions)
    return resolve_catalog(descriptors_from_items(items))


def referenced_node_type_versions(definition: Any,
                                  node_type_items: Sequence[Dict]
                                  ) -> Dict[str, int]:
    """The Custom_Node_Type versions a Workflow_Definition uses (14.2).

    Returns ``{typeId: version}`` for every canvas node whose type is a
    registered Custom_Node_Type, recording the latest registered version
    at save time (the version the palette served). Built-in type ids are
    never treated as custom references.
    """
    if not isinstance(definition, dict):
        return {}
    nodes = definition.get('nodes')
    if not isinstance(nodes, list):
        return {}
    latest = latest_versions(node_type_items)
    references: Dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get('type')
        if node_type in BUILTIN_TYPE_IDS or node_type not in latest:
            continue
        references[node_type] = int(latest[node_type].get('version', 0))
    return references


# ----------------------------------------------------------- AWS loaders
#
# Thin persistence layer over the node-designer tables. Everything above
# stays pure; everything below degrades to the built-in catalog when the
# node-designer stack (CUSTOM_NODE_TYPES_TABLE / PLUGIN_RECORDS_TABLE) is
# not deployed alongside the workflow handlers.

import boto3  # noqa: E402  (kept below the pure section deliberately)
from botocore.exceptions import ClientError  # noqa: E402

dynamodb = boto3.resource('dynamodb')

CUSTOM_NODE_TYPES_TABLE = os.environ.get('CUSTOM_NODE_TYPES_TABLE')
PLUGIN_RECORDS_TABLE = os.environ.get('PLUGIN_RECORDS_TABLE')


def _decimal_to_native(obj):
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def load_registered_node_types(usecase_id: str) -> List[Dict]:
    """Every CustomNodeTypes version item visible to a Use_Case: items it
    owns plus items scoped to it at registration (usecase_ids, 8.2).

    A type may be owned by one Use_Case and scoped to others, which the
    owning-Use_Case GSI cannot answer, so this scans the (small) catalog
    table with a containment filter. Returns [] when the node-designer
    stack is not deployed, so consumers fall back to the built-in
    catalog unchanged.
    """
    if not CUSTOM_NODE_TYPES_TABLE or not usecase_id:
        return []
    from boto3.dynamodb.conditions import Attr
    table = dynamodb.Table(CUSTOM_NODE_TYPES_TABLE)
    items: List[Dict] = []
    kwargs: Dict = {
        'FilterExpression': (Attr('usecase_id').eq(usecase_id)
                             | Attr('usecase_ids').contains(usecase_id)),
    }
    try:
        while True:
            response = table.scan(**kwargs)
            items.extend(response.get('Items', []))
            last = response.get('LastEvaluatedKey')
            if not last:
                break
            kwargs['ExclusiveStartKey'] = last
    except ClientError as e:
        logger.warning("Could not load custom node types for use case %s: %s",
                       usecase_id, str(e))
        return []
    return [_decimal_to_native(item) for item in items]


def load_lifecycle_states(node_type_items: Sequence[Dict]
                          ) -> Dict[Tuple[str, int], str]:
    """Lifecycle_State of each distinct backing Plugin_Record version
    pinned by the given items. Missing records or an undeployed
    PluginRecords table yield no entry (the palette fails closed)."""
    if not PLUGIN_RECORDS_TABLE:
        return {}
    table = dynamodb.Table(PLUGIN_RECORDS_TABLE)
    keys = set()
    for item in node_type_items:
        plugin_id = item.get('plugin_id')
        plugin_version = item.get('plugin_version')
        if plugin_id is not None and plugin_version is not None:
            keys.add((plugin_id, int(plugin_version)))
    states: Dict[Tuple[str, int], str] = {}
    for plugin_id, plugin_version in keys:
        try:
            response = table.get_item(
                Key={'plugin_id': plugin_id, 'version': plugin_version})
        except ClientError as e:
            logger.warning("Could not load plugin record %s v%s: %s",
                           plugin_id, plugin_version, str(e))
            continue
        record = response.get('Item')
        if record and record.get('lifecycle_state'):
            states[(plugin_id, plugin_version)] = str(record['lifecycle_state'])
    return states


def palette_catalog_for_usecase(usecase_id: Optional[str]
                                ) -> Tuple[tuple, Dict[str, str]]:
    """The merged palette catalog served for one Use_Case (9.2, 9.6,
    14.3). Without a Use_Case (or without the node-designer stack) the
    built-in catalog is served unchanged."""
    if not usecase_id:
        return NODE_CATALOG, {}
    items = load_registered_node_types(usecase_id)
    if not items:
        return NODE_CATALOG, {}
    return resolve_palette_catalog(items, load_lifecycle_states(items))


def resolution_catalog_for_usecase(usecase_id: Optional[str],
                                   pinned_versions: Optional[Dict[str, Any]] = None
                                   ) -> tuple:
    """The merged catalog a Use_Case's existing workflows validate/load
    against, honoring versions pinned at save (14.2, 14.3)."""
    if not usecase_id:
        return NODE_CATALOG
    items = load_registered_node_types(usecase_id)
    if not items:
        return NODE_CATALOG
    return resolve_resolution_catalog(items, pinned_versions)
