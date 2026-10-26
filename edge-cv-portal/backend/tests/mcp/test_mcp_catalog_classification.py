"""Portal classification honesty (`test_mcp_catalog_classification.py`).

Spec: .kiro/specs/mcp-servers/ (design.md Decision point 4 "Classification
rules" and "Accepted reading — status sync"; §2 `catalog.py` row,
classification rule; Testing Strategy row "Portal classification honesty";
tasks.md 5.8). Requirements 5.1, 5.2, 1.2.

For every `RO` portal spec with a backing route, the walk of
`tests/mcp/_handler_ast.py` covers `handler_function` and every function it
reaches in its own module and the other `functions/*.py` modules, and fails
on any write call (the named verbs and the ten verb prefixes on a receiver
that is not an imported module, and the derived `SHARED_LAYER_WRITERS`) that
is not one of the spec's `accepted_writes`, matched on `(function, call,
receiver source text)` with exactly the entry's count. There is no
audit-result clause.
"""
import dataclasses

import pytest

import _handler_ast as h
from mcp_portal import catalog as cat
from mcp_portal.catalog import STATUS_SYNC_NOTE, AcceptedWrite, ToolSpec, schema

RO_ROUTED = [s for s in cat.CATALOG if s.read_only and s.backend is not None]
BY_NAME = {s.name: s for s in cat.CATALOG}

# Decision point 4: the fourteen derived writers at the spec's pin, the
# seven revision-9 names among them. The derivation at the branch base also
# finds `shared_utils.attach_bucket` (use-case bucket configuration, added
# after the pin); containment, not equality, is the contract.
REVISION_9_WRITERS = (
    'shared_utils.record_audit_event_strict',
    'shared_utils.finalize_audit_event',
    'shared_utils.RBACManager.assign_user_role',
    'shared_utils.RBACManager.remove_user_role',
    'detector_conversion.reconcile_conversion',
    'detector_conversion.apply_conversion_transition',
    'detector_conversion.invoke_finalize',
)
PIN_WRITERS = REVISION_9_WRITERS + (
    's3_cors.ensure_bucket_cors',
    'user_roles_dao.UserRolesDAO.assign_user_to_usecase',
    'user_roles_dao.UserRolesDAO.bulk_assign_users',
    'user_roles_dao.UserRolesDAO.update_user_role',
    'user_roles_dao.UserRolesDAO.remove_user_from_usecase',
    'user_roles_dao.UserRolesDAO.remove_all_user_assignments',
    'user_roles_dao.UserRolesDAO.remove_all_usecase_assignments',
)

# The ten status-sync tools and their eleven entries (twelve call sites).
STATUS_SYNC = {
    'portal_models_get': {('models.get_model', 'update_item', 'training_table'): 1},
    'portal_models_training_get': {
        ('training.get_training_job', 'update_item', 'table'): 2,
        ('training.sync_conversion_record', 'reconcile_conversion', ''): 1},
    'portal_models_compilation_get': {
        ('compilation.get_compilation_status', 'update_item', 'table'): 1},
    'portal_data_labeling_jobs_list': {
        ('labeling.list_labeling_jobs', 'update_item', 'labeling_jobs_table'): 1},
    'portal_data_labeling_job_get': {
        ('labeling.get_labeling_job', 'update_item', 'labeling_jobs_table'): 1},
    'portal_workflows_generation_job_get': {
        ('workflow_generator.record_job_failure', 'update_item',
         'dynamodb.Table(WORKFLOW_CHAT_SESSIONS_TABLE)'): 1},
    'portal_workflows_test_run_get': {
        ('workflow_testing.sync_run_status_from_execution', 'update_item',
         'dynamodb.Table(TEST_RUNS_TABLE)'): 1},
    'portal_workflows_simulation_get': {
        ('plugin_simulator.sync_run_status_from_execution', 'update_item',
         'runs_table()'): 1},
    'portal_workflows_plugin_modules_list': {
        ('plugin_importer.write_module_index_cache', 'put_item',
         'module_cache_table()'): 1},
    'portal_workflows_build_servers_list': {
        ('build_fleet.apply_observed_state', 'update_item', 'servers_table()'): 1},
}

AUDITED_READS = (
    ('devices', 'list_devices'), ('devices', 'get_device'),
    ('device_logs', 'list_log_groups'), ('device_logs', 'get_component_logs'),
    ('deployments', 'get_deployment'), ('data_management', 'list_buckets'),
    ('usecases', 'list_usecases'), ('usecases', 'get_usecase'),
)
POST_READS = (
    ('model_import', 'validate_model'), ('model_converter', 'inspect_model_endpoint'),
    ('datasets', 'count_images'), ('pre_labeled_datasets', 'validate_manifest'),
    ('device_logs_analyzer', 'analyze_device_logs'),
)


def classification_problems(spec: ToolSpec) -> list:
    """Every reason `spec` may not be `RO`; empty when it may."""
    problems = []
    if spec.accepted_writes and spec.accepted_writes_note != STATUS_SYNC_NOTE:
        problems.append(f'{spec.name}: accepted_writes without STATUS_SYNC_NOTE')
    accepted = {(a.function, a.call, a.receiver): a.count for a in spec.accepted_writes}
    found = {}
    for write in h.spec_writes(spec):
        found.setdefault(write.key, []).append(write)
    for key, writes in sorted(found.items()):
        if key not in accepted:
            for write in writes:
                problems.append(
                    f'{spec.name}: write call {write.call!r} on receiver '
                    f'{write.receiver!r} in {write.function.key} at line {write.lineno}')
        elif accepted[key] != len(writes):
            lines = sorted(w.lineno for w in writes)
            problems.append(
                f'{spec.name}: accepted write {key} found {len(writes)} time(s) '
                f'(lines {lines}), entry count {accepted[key]}')
    for key, count in sorted(accepted.items()):
        if key not in found:
            problems.append(f'{spec.name}: accepted write {key} (count {count}) not found')
    return problems


def fixture_spec(module: str, function: str, **overrides) -> ToolSpec:
    values = dict(
        name='portal_devices_fixture_get', domain='devices', title='Fixture',
        description='A fixture spec.', resource_type='fixture',
        input_schema=schema(), backend='fixture', handler_module=module,
        handler_function=function, method='GET', resource='/fixture',
        permissions=(), scope='global')
    values.update(overrides)
    return ToolSpec(**values)


@pytest.mark.parametrize('spec', RO_ROUTED, ids=lambda s: s.name)
def test_read_only_spec_has_no_unaccepted_write(spec):
    assert classification_problems(spec) == []


def test_every_read_only_routed_spec_was_walked():
    # 89 RO portal tools by revision 9, 88 since the manifest read left CATALOG.
    assert len(RO_ROUTED) == 88


def test_shared_layer_writers_are_derived_and_contain_the_named_set():
    derived = set(h.shared_layer_writers())
    assert set(REVISION_9_WRITERS) <= derived
    assert set(PIN_WRITERS) <= derived
    assert 'shared_utils.log_audit_event' not in derived


def test_derivation_skips_vendored_layer_modules():
    layer = {name for name, mod in h.universe().modules.items() if mod.layer}
    assert 'six' not in layer and 'shared_utils' in layer


def test_status_sync_tools_carry_exactly_their_entries():
    for name, entries in STATUS_SYNC.items():
        spec = BY_NAME[name]
        assert spec.read_only, name
        assert spec.accepted_writes_note == STATUS_SYNC_NOTE, name
        assert {(a.function, a.call, a.receiver): a.count
                for a in spec.accepted_writes} == entries, name
    assert sum(len(e) for e in STATUS_SYNC.values()) == 11
    assert sum(sum(e.values()) for e in STATUS_SYNC.values()) == 12
    carrying = {s.name for s in cat.CATALOG if s.accepted_writes}
    assert carrying == set(STATUS_SYNC)


@pytest.mark.parametrize('name', sorted(STATUS_SYNC))
def test_removing_an_accepted_entry_fails_naming_tool_and_verb(name):
    spec = BY_NAME[name]
    for index, entry in enumerate(spec.accepted_writes):
        rest = spec.accepted_writes[:index] + spec.accepted_writes[index + 1:]
        variant = dataclasses.replace(spec, accepted_writes=rest)
        problems = classification_problems(variant)
        assert problems, (name, entry)
        assert all(name in p for p in problems)
        assert any(repr(entry.call) in p and 'line' in p for p in problems)


@pytest.mark.parametrize('name', sorted(STATUS_SYNC))
def test_changing_an_entry_count_fails(name):
    spec = BY_NAME[name]
    entry = spec.accepted_writes[0]
    bumped = dataclasses.replace(entry, count=entry.count + 1)
    variant = dataclasses.replace(spec, accepted_writes=(bumped,) + spec.accepted_writes[1:])
    assert any('entry count' in p for p in classification_problems(variant))


def test_stale_accepted_entry_fails():
    spec = BY_NAME['portal_models_get']
    stale = AcceptedWrite('models.get_model', 'put_item', 'training_table')
    variant = dataclasses.replace(spec, accepted_writes=spec.accepted_writes + (stale,))
    assert any('not found' in p for p in classification_problems(variant))


def test_accepted_writes_require_the_status_sync_note():
    spec = BY_NAME['portal_models_get']
    variant = dataclasses.replace(spec, accepted_writes_note='other')
    assert any('STATUS_SYNC_NOTE' in p for p in classification_problems(variant))


def test_validate_workflow_is_not_read_only():
    # The row that was RO/Phase 1 for six reviews: s3.put_object and
    # dynamodb.Table(...).update_item on its path.
    spec = fixture_spec('workflow_validation', 'validate_workflow')
    calls = {(w.call, w.receiver) for w in h.spec_writes(spec)}
    assert ('put_object', 's3') in calls
    assert ('update_item', 'dynamodb.Table(WORKFLOW_VERSIONS_TABLE)') in calls
    assert classification_problems(spec)
    assert not BY_NAME['portal_workflows_validate'].read_only


@pytest.mark.parametrize('module,function', POST_READS + AUDITED_READS)
def test_post_reads_and_audited_reads_are_read_only(module, function):
    # No audit-result clause: the eight audited reads write 'success'
    # entries and stay RO; the POST reads change no state.
    assert classification_problems(fixture_spec(module, function)) == []


def test_audited_reads_do_call_the_audit_writer():
    for module, function in AUDITED_READS:
        graph = h.spec_graph(fixture_spec(module, function))
        uni = h.universe()
        names = {getattr(c.func, 'id', getattr(c.func, 'attr', None))
                 for ref in graph for c in uni.calls(ref)}
        assert 'log_audit_event' in names, (module, function)


def test_user_roles_dao_writer_fails_a_read_only_fixture():
    spec = fixture_spec('user_roles', 'assign_user_to_usecase')
    problems = classification_problems(spec)
    assert any("'assign_user_to_usecase'" in p for p in problems)


def test_call_through_imported_module_object_is_followed_not_counted():
    # camera_video_pin.handler: camera_registry.create_response(404, ...).
    uni = h.universe()
    spec = fixture_spec('camera_video_pin', 'handler')
    pin = uni.module('camera_video_pin')
    assert pin.imports['camera_registry'] == ('module', 'camera_registry')
    sites = [c for c in uni.calls(h.FuncRef('camera_video_pin', 'handler'))
             if isinstance(c.func, h.ast.Attribute) and c.func.attr == 'create_response']
    assert sites and all(c.func.value.id == 'camera_registry' for c in sites)
    assert not any(w.call == 'create_response' for w in h.spec_writes(spec))
    # The forwarding shim is followed into camera_registry.handler.
    assert h.FuncRef('camera_registry', 'handler') in h.spec_graph(spec)
