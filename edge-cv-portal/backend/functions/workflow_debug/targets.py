"""The ``all_devices`` target set of a debug session (workflow-debug-mode
design §7.2).

``registered_devices(table, usecase_id, workflow_id)`` reproduces
``workflow_tuning.registered_devices`` over an injected Deployments table: a
``Query`` on ``usecase-deployments-index`` by ``usecase_id``, keeping the
deployments whose ``deployment_status`` (upper-cased) is one of
``ACTIVE_DEPLOYMENT_STATUSES`` and that reference the workflow (``component_type
== 'workflow'`` with the ``workflow_id``, or a component named
``dda.workflow.{workflow_id}`` in ``components``), collecting
``target_devices`` and ``target_device_names``, de-duplicated and sorted,
paginating. It is written here rather than imported because
``workflow_tuning``'s module-level imports reach into the ``workflow_core``
layer beyond ``stream_url`` (which a Python 3.12 function must not import)
and into the ComputeStack's own code asset; a unit test asserts the two
functions agree on shared fixtures and that the two status tuples are equal.

``target_set`` intersects the result with the use case's Device_Records and
names what is missing on each side.
"""
from collections import namedtuple
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["ACTIVE_DEPLOYMENT_STATUSES", "TargetSet", "registered_devices", "target_set"]

#: Deployment statuses that still put a workflow on a device; equal to
#: ``workflow_tuning.ACTIVE_DEPLOYMENT_STATUSES`` (a unit test pins it).
ACTIVE_DEPLOYMENT_STATUSES = ('ACTIVE', 'COMPLETED', 'IN_PROGRESS',
                              'PENDING', 'QUEUED', 'DEPLOYED')

#: ``devices``: the sorted target set; ``no_deployment``: recorded devices
#: without an active deployment of the workflow; ``no_device_record``:
#: deployment targets without a Device_Record naming the use case;
#: ``over_cap``: more devices than the session cap allows.
TargetSet = namedtuple("TargetSet", ["devices", "no_deployment", "no_device_record", "over_cap"])


def _references_workflow(deployment: Dict[str, Any], workflow_id: str, component_name: str) -> bool:
    references = (
        deployment.get('component_type') == 'workflow'
        and str(deployment.get('workflow_id') or '') == workflow_id)
    for component in deployment.get('components') or []:
        if isinstance(component, dict):
            if component.get('component_name') == component_name:
                references = True
        elif component == component_name:
            references = True
    return references


def registered_devices(table: Any, usecase_id: str, workflow_id: str) -> List[str]:
    """Devices the Portal records as running the workflow (sorted, unique)."""
    component_name = "dda.workflow.{0}".format(workflow_id)
    devices: List[str] = []
    kwargs: Dict[str, Any] = {
        'IndexName': 'usecase-deployments-index',
        'KeyConditionExpression': 'usecase_id = :uid',
        'ExpressionAttributeValues': {':uid': usecase_id},
    }
    while True:
        response = table.query(**kwargs)
        for deployment in response.get('Items', []) or []:
            status = str(deployment.get('deployment_status') or '').upper()
            if status not in ACTIVE_DEPLOYMENT_STATUSES:
                continue
            if not _references_workflow(deployment, workflow_id, component_name):
                continue
            for device in deployment.get('target_devices') or []:
                if device and str(device) not in devices:
                    devices.append(str(device))
            for device in deployment.get('target_device_names') or []:
                if device and str(device) not in devices:
                    devices.append(str(device))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return sorted(devices)


def target_set(registered: Iterable[str], recorded: Iterable[str], *,
               max_devices: Optional[int] = None) -> TargetSet:
    """``registered ∩ recorded`` with the reasons for what fell out."""
    registered_set = {str(d) for d in registered if d}
    recorded_set = {str(d) for d in recorded if d}
    devices = sorted(registered_set & recorded_set)
    over_cap = max_devices is not None and len(devices) > int(max_devices)
    return TargetSet(
        devices=devices,
        no_deployment=sorted(recorded_set - registered_set),
        no_device_record=sorted(registered_set - recorded_set),
        over_cap=over_cap,
    )
