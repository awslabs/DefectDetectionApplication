"""`handler_audits` honesty (`test_mcp_handler_audit_flags.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `catalog.py` row,
`handler_audits` / `handler_audits_denials`; Testing Strategy row
"`handler_audits` honesty"; tasks.md 5.9). Requirements 4.1, 4.3, 16.4.

For each spec with a backing route, the `log_audit_event` /
`record_audit_event_strict` calls on its call graph (the classification walk
of `_handler_ast.py`: its module and the other `functions/*.py` modules) are
classified by their `result` argument (5th positional or `result=`):
`success_path` for the literal `'success'`/`'pending'`, an absent argument
or any non-literal expression; `denial` for the literal `'denied'` in a
function that itself builds a 403; ignored for `'failure'`/`'failed'`/
`'rejected'` (and any other literal). The flags must equal
`handler_audits == (success_path >= 1)` and `handler_audits_denials ==
(decorated with an audited-denial decorator or denial >= 1)`, except for the
specs pinned in `PINNED_AUDIT_FLAGS` with their reason.
"""
import pytest

import _handler_ast as h
from mcp_portal import catalog as cat

ROUTED = [s for s in cat.CATALOG if s.backend is not None]
BY_NAME = {s.name: s for s in cat.CATALOG}

# A spec whose success path audits on some branches only is pinned
# `handler_audits=False`, so the envelope always records the call (§2
# `catalog.py` row; the documented exception to the `max(k, 1)` count).
PINNED_AUDIT_FLAGS = {
    'portal_devices_shared_components_provision': (
        False, 'only its daemon thread writes the success entry'),
    'portal_data_labeling_job_create': (
        False, 'only the DDA labeling backend audits; the other backends do not'),
    'portal_data_synthetic_previews_approve': (
        False, "only an 'approved' decision audits"),
    'portal_workflows_plugin_generate_message': (
        False, 'only an accept turn audits'),
}


def computed_flags(module: str, function: str):
    uni = h.universe()
    start = uni.find_function(module, function)
    assert start is not None, (module, function)
    graph = uni.reach(start)
    calls = h.audit_calls(graph)
    audits = any(c.kind == 'success_path' for c in calls)
    denials = h.denial_decorated(uni.node(start)) or any(c.kind == 'denial' for c in calls)
    return audits, denials, calls


def describe(calls) -> str:
    return '; '.join(f'{c.function.key}:{c.lineno} result={c.result!r} -> {c.kind}'
                     for c in calls) or 'no audit call'


@pytest.mark.parametrize('spec', ROUTED, ids=lambda s: s.name)
def test_audit_flags_match_the_handler(spec):
    audits, denials, calls = computed_flags(spec.handler_module, spec.handler_function)
    if spec.name in PINNED_AUDIT_FLAGS:
        pinned, reason = PINNED_AUDIT_FLAGS[spec.name]
        assert reason
        assert spec.handler_audits == pinned, f'{spec.name}: pinned {pinned} ({reason})'
        # The pin is needed: the walk finds an auditing branch.
        assert audits, f'{spec.name}: pinned but no success-path audit found'
    else:
        assert spec.handler_audits == audits, (
            f'{spec.name}: handler_audits={spec.handler_audits}, computed {audits}: '
            f'{describe(calls)}')
    assert spec.handler_audits_denials == denials, (
        f'{spec.name}: handler_audits_denials={spec.handler_audits_denials}, '
        f'computed {denials}: {describe(calls)}')


def test_pinned_specs_exist():
    assert set(PINNED_AUDIT_FLAGS) <= set(BY_NAME)


# ------------------------------------------------- fixed regression cases

@pytest.mark.parametrize('module,function,expected', [
    # only authorize()'s 'denied' call
    ('camera_registry', 'get_cameras', (False, True)),
    ('camera_registry', 'get_conflicts', (False, True)),
    # no audit call at all
    ('deployments', 'list_deployments', (False, False)),
    # a success entry
    ('devices', 'list_devices', (True, None)),
    ('deployments', 'get_camera_binding_context', (False, True)),
    # revision 9
    ('camera_registry', 'remove_static_video_pin', (True, True)),
    ('user_admin', 'list_accounts', (None, True)),
    # `_refused_model_source`'s 'denied' entry belongs to a 400
    ('model_converter', 'inspect_model_endpoint', (None, False)),
    ('model_converter', 'convert_model', (None, False)),
    # two calls down
    ('build_fleet', 'start_build_server', (True, None)),
    ('build_fleet', 'stop_build_server', (True, None)),
    ('build_fleet', 'terminate_build_server', (True, None)),
    ('workflow_validation', 'validate_workflow', (None, True)),
])
def test_fixed_cases(module, function, expected):
    audits, denials, calls = computed_flags(module, function)
    if expected[0] is not None:
        assert audits == expected[0], describe(calls)
    if expected[1] is not None:
        assert denials == expected[1], describe(calls)


def test_fail_conversion_alone_is_not_a_success_audit():
    # A route whose only audit helper is packaging._fail_conversion records
    # failures only.
    audits, _denials, calls = computed_flags('packaging', '_fail_conversion')
    assert calls and not audits, describe(calls)


def test_require_portal_admin_counts_as_an_audited_denial_decorator():
    uni = h.universe()
    node = uni.node(uni.find_function('user_admin', 'list_accounts'))
    assert h.denial_decorated(node)


def test_refused_model_source_denial_is_not_a_403_denial():
    uni = h.universe()
    ref = uni.find_function('model_converter', '_refused_model_source')
    calls = h.audit_calls([ref])
    assert any(c.result == 'denied' for c in calls)
    assert not any(c.kind == 'denial' for c in calls)


def test_shared_components_provision_is_pinned_false():
    assert BY_NAME['portal_devices_shared_components_provision'].handler_audits is False
    audits, _d, _c = computed_flags('shared_components', 'provision_components')
    assert audits
