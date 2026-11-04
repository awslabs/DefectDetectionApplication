"""The Journey (design D10, §5; Requirement 4.2): the eight ordered steps of
the user guide's "end-to-end path", the probes that tell whether a step is
met for a Use_Case, and the pure `next_step` over their summary.
"""
from typing import Any, Callable, Dict, List, Optional

# Summary values per probe key.
SOME = 'some'
NONE = 'none'
NO_PERMISSION = 'no_permission'
ERROR = 'error'
UNKNOWN_VALUES = (NO_PERMISSION, ERROR)

# number, title, Portal page label, route, guide chapter, the summary keys
# that must all be SOME for the step to count as done (empty: the use case
# itself, met whenever a usecase_id resolves).
JOURNEY_STEPS: List[Dict[str, Any]] = [
    {'number': 1, 'title': 'Create the use case and add the team', 'page': 'Use Cases',
     'route': '/usecases', 'chapter': '02-use-cases.md', 'keys': ()},
    {'number': 2, 'title': 'Upload or browse images', 'page': 'Data Management',
     'route': '/data', 'chapter': '03-data-management.md', 'keys': ('datasets',)},
    {'number': 3, 'title': 'Label the images', 'page': 'Labeling', 'route': '/labeling',
     'chapter': '04-labeling.md', 'keys': ('labeling_jobs',)},
    {'number': 4, 'title': 'Train a model', 'page': 'Training', 'route': '/training',
     'chapter': '06-training.md', 'keys': ('trained_models',)},
    {'number': 5, 'title': 'Build a workflow', 'page': 'Workflows', 'route': '/workflows/builder',
     'chapter': '08-workflows.md', 'keys': ('workflows',)},
    {'number': 6, 'title': 'Build the LocalServer component', 'page': 'Builds',
     'route': '/builds', 'chapter': '12-builds.md', 'keys': ('builds',)},
    {'number': 7, 'title': 'Register the device and deploy', 'page': 'Devices',
     'route': '/devices', 'chapter': '15-devices.md', 'keys': ('devices', 'deployments')},
    {'number': 8, 'title': 'Run and review inspections', 'page': 'Workflow dashboards',
     'route': '/workflow-dashboards', 'chapter': '14-workflow-dashboards.md', 'keys': ()},
]

# probe key -> (catalog tool, arguments builder, result reader)
PROBES: Dict[str, Dict[str, Any]] = {
    'datasets': {'tool': 'portal_data_datasets_count',
                 'args': lambda uc: {'usecase_id': uc, 'prefix': ''}},
    'labeling_jobs': {'tool': 'portal_data_labeling_jobs_list',
                      'args': lambda uc: {'usecase_id': uc, 'limit': 1}},
    'trained_models': {'tool': 'portal_models_training_list',
                       'args': lambda uc: {'usecase_id': uc, 'limit': 1}},
    'models': {'tool': 'portal_models_list', 'args': lambda uc: {'usecase_id': uc, 'limit': 1}},
    'workflows': {'tool': 'portal_workflows_list',
                  'args': lambda uc: {'usecase_id': uc, 'limit': 1}},
    'builds': {'tool': 'portal_workflows_builds_list', 'args': lambda uc: {'limit': 1}},
    'devices': {'tool': 'portal_devices_list', 'args': lambda uc: {'usecase_id': uc, 'limit': 1}},
    'deployments': {'tool': 'portal_devices_deployments_list',
                    'args': lambda uc: {'usecase_id': uc, 'limit': 1}},
}
PROBE_KEYS = tuple(PROBES)


def has_items(result: Any) -> bool:
    """Whether a shaped tool result shows at least one item: a non-empty list
    anywhere at the top level, or a positive count-like integer."""
    if isinstance(result, list):
        return len(result) > 0
    if not isinstance(result, dict):
        return False
    for key, value in result.items():
        if isinstance(value, list) and value:
            return True
        if key in ('count', 'total', 'total_hint', 'total_count', 'image_count',
                   'object_count', 'returned') and isinstance(value, (int, float)) \
                and not isinstance(value, bool) and value > 0:
            return True
    return False


def summary_from_outcomes(outcomes: Dict[str, Any]) -> Dict[str, str]:
    """`outcomes` maps a probe key to a `ToolOutcome` (or None when the probe
    was not run); the summary maps it to SOME/NONE/NO_PERMISSION/ERROR."""
    summary: Dict[str, str] = {}
    for key in PROBE_KEYS:
        outcome = outcomes.get(key)
        if outcome is None:
            summary[key] = ERROR
        elif outcome.failure is not None:
            summary[key] = NO_PERMISSION if outcome.failure.category == 'not_authorized' else ERROR
        else:
            result = outcome.shaped.result if outcome.shaped is not None else None
            summary[key] = SOME if has_items(result) else NONE
    # a trained model or an imported one both satisfy step 4
    if summary.get('trained_models') != SOME and summary.get('models') == SOME:
        summary['trained_models'] = SOME
    return summary


def next_step(summary: Dict[str, str]) -> Dict[str, Any]:
    """The first Journey_Step whose keys are not all SOME. Unknown values
    (`no_permission`, `error`) never advance a step: the step is reported
    with `unknown` naming the keys that could not be read."""
    for step in JOURNEY_STEPS:
        keys = step['keys']
        if not keys:
            if step['number'] == 8:
                return {**_public(step), 'unknown': [], 'done': True}
            continue
        values = [summary.get(k, ERROR) for k in keys]
        if all(v == SOME for v in values):
            continue
        unknown = [k for k, v in zip(keys, values) if v in UNKNOWN_VALUES]
        return {**_public(step), 'unknown': unknown, 'done': False}
    final = JOURNEY_STEPS[-1]
    return {**_public(final), 'unknown': [], 'done': True}


def _public(step: Dict[str, Any]) -> Dict[str, Any]:
    return {k: step[k] for k in ('number', 'title', 'page', 'route', 'chapter')}


def run_probes(usecase_id: str, call: Callable[[str, Dict[str, Any]], Any]) -> Dict[str, Any]:
    """Run every probe through `call(tool_name, args)`; returns key -> outcome."""
    return {key: call(probe['tool'], probe['args'](usecase_id)) for key, probe in PROBES.items()}


def status(usecase_id: str, call: Callable[[str, Dict[str, Any]], Any]) -> Dict[str, Any]:
    outcomes = run_probes(usecase_id, call)
    summary = summary_from_outcomes(outcomes)
    return {'usecase_id': usecase_id, 'summary': summary, 'next_step': next_step(summary),
            'steps': [_public(s) for s in JOURNEY_STEPS]}


def step_by_number(number: int) -> Optional[Dict[str, Any]]:
    for step in JOURNEY_STEPS:
        if step['number'] == number:
            return _public(step)
    return None
