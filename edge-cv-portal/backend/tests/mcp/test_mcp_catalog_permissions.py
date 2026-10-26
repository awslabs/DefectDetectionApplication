"""Catalog <= handler permissions (`test_mcp_catalog_permissions.py`).

Spec: .kiro/specs/mcp-servers/ (design.md §2 `catalog.py` row, "The declared
`permissions` admit a role set that is a subset of the set the handler
admits, the handler's set being recorded per row"; Property 2; Testing
Strategy row "Catalog <= handler permissions"; tasks.md 5.11).
Requirements 1.3, 3.1, 3.5, 3.7, 3.9.

Table-driven: for each portal spec with a backing route, the roles whose
`role_permissions` hold every declared permission must be a subset of the
handler-admitted role set recorded below. The recorded sets were taken per
row from the handler's own check (decorator, `has_permission`,
`check_user_access` with its role hierarchy, the module's authorize helper,
or none), at the branch base: `usecases.create_usecase` now checks
`CREATE_USECASES` at `'global'`, and `shared_components.provision_components`
requires PortalAdmin on the named Use_Case.
"""
import dataclasses
import sys

import pytest

from mcp_portal import catalog as cat

BY_NAME = {s.name: s for s in cat.CATALOG}
ROUTED = [s for s in cat.CATALOG if s.backend is not None]

ANY = frozenset({'Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin',
                 'DataLabeler'})
VIEW = frozenset({'Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'})
OPERATE = frozenset({'Operator', 'UseCaseAdmin', 'PortalAdmin'})
OPERATE_DS = frozenset({'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'})
SCIENCE = frozenset({'DataScientist', 'UseCaseAdmin', 'PortalAdmin'})
LABEL = frozenset({'DataLabeler', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'})
ADMIN = frozenset({'UseCaseAdmin', 'PortalAdmin'})
PORTAL = frozenset({'PortalAdmin'})

# Handler-admitted roles per tool. `portal_admin_audit_logs_query` records
# the set the handler serves every entry to (PortalAdmin); other roles get a
# view filtered to their own Use_Cases, which Requirement 3.7 does not cover.
HANDLER_ROLES = {
    'portal_admin_account_role_change': PORTAL,
    'portal_admin_accounts_list': PORTAL,
    'portal_admin_audit_logs_query': PORTAL,
    'portal_admin_edge_sync_device_sync': PORTAL,
    'portal_admin_edge_sync_devices_list': PORTAL,
    'portal_admin_role_assign': PORTAL,
    'portal_admin_role_remove': PORTAL,
    'portal_admin_usecase_create': PORTAL,
    'portal_admin_usecase_delete': ADMIN,
    'portal_admin_usecase_get': VIEW,
    'portal_admin_usecase_update': ADMIN,
    'portal_admin_usecases_list': ANY,
    'portal_admin_user_permissions_get': PORTAL,
    'portal_admin_users_list': ADMIN,
    'portal_data_buckets_list': ANY,
    'portal_data_captures_list': ANY,
    'portal_data_configure': ANY,
    'portal_data_datasets_count': ANY,
    'portal_data_datasets_list': ANY,
    'portal_data_datasets_preview_get': ANY,
    'portal_data_folder_create': SCIENCE,
    'portal_data_folders_list': ANY,
    'portal_data_labeler_jobs_list': LABEL,
    'portal_data_labeler_pool_get': LABEL,
    'portal_data_labeler_task_image_url_get': LABEL,
    'portal_data_labeler_task_next': LABEL,
    'portal_data_labeler_task_presentation_failure': LABEL,
    'portal_data_labeler_task_steal': LABEL,
    'portal_data_labeler_task_submit': LABEL,
    'portal_data_labeling_job_create': ANY,
    'portal_data_labeling_job_delete': SCIENCE,
    'portal_data_labeling_job_get': ANY,
    'portal_data_labeling_job_stop': SCIENCE,
    'portal_data_labeling_jobs_list': ANY,
    'portal_data_labeling_prelabels_rerun': SCIENCE,
    'portal_data_labeling_preview_get': SCIENCE,
    'portal_data_labeling_preview_run': SCIENCE,
    'portal_data_labeling_review_decide': ADMIN,
    'portal_data_labeling_review_finalize': ADMIN,
    'portal_data_labeling_review_get': ADMIN,
    'portal_data_labeling_team_create': ADMIN,
    'portal_data_labeling_team_delete': ADMIN,
    'portal_data_labeling_team_member_add': ADMIN,
    'portal_data_labeling_team_member_remove': ADMIN,
    'portal_data_labeling_teams_list': ADMIN,
    'portal_data_manifest_validate': SCIENCE,
    'portal_data_prelabeled_create': SCIENCE,
    'portal_data_prelabeled_delete': SCIENCE,
    'portal_data_prelabeled_get': ANY,
    'portal_data_prelabeled_list': ANY,
    'portal_data_synthetic_models_list': SCIENCE,
    'portal_data_synthetic_previews_approve': SCIENCE,
    'portal_data_synthetic_prompt_templates_get': SCIENCE,
    'portal_data_synthetic_prompt_templates_set': SCIENCE,
    'portal_data_synthetic_session_create': SCIENCE,
    'portal_data_synthetic_session_generate': SCIENCE,
    'portal_data_synthetic_session_get': SCIENCE,
    'portal_data_synthetic_session_integrate': SCIENCE,
    'portal_data_synthetic_session_retrain': SCIENCE,
    'portal_data_synthetic_session_update': SCIENCE,
    'portal_data_synthetic_sessions_list': SCIENCE,
    'portal_data_workteams_list': ANY,
    'portal_devices_camera_add': OPERATE,
    'portal_devices_camera_delete': OPERATE,
    'portal_devices_camera_update': OPERATE,
    'portal_devices_cameras_conflicts_get': VIEW,
    'portal_devices_cameras_list': VIEW,
    'portal_devices_cameras_refresh': VIEW,
    'portal_devices_component_delete': ADMIN,
    'portal_devices_components_get': ANY,
    'portal_devices_components_list': ANY,
    'portal_devices_components_schema_get': ANY,
    'portal_devices_delete': OPERATE,
    'portal_devices_deployment_cancel': ANY,
    'portal_devices_deployment_create': ANY,
    'portal_devices_deployment_for_target_get': ANY,
    'portal_devices_deployment_get': ANY,
    'portal_devices_deployments_list': ANY,
    'portal_devices_flags_update': ADMIN,
    'portal_devices_get': ANY,
    'portal_devices_list': ANY,
    'portal_devices_logs_analyze': ANY,
    'portal_devices_logs_get': ANY,
    'portal_devices_logs_list': ANY,
    'portal_devices_registration_delete': OPERATE,
    'portal_devices_registrations_list': ANY,
    'portal_devices_shared_components_available_list': ANY,
    'portal_devices_shared_components_list': ANY,
    'portal_devices_shared_components_provision': PORTAL,
    'portal_devices_shared_components_status_get': PORTAL,
    'portal_devices_shared_components_update_all': PORTAL,
    'portal_devices_static_image_get': VIEW,
    'portal_devices_static_image_unpin': OPERATE,
    'portal_devices_static_video_get': VIEW,
    'portal_devices_static_video_unpin': OPERATE,
    'portal_devices_thing_groups_list': ANY,
    'portal_models_compilation_create': SCIENCE,
    'portal_models_compilation_get': ANY,
    'portal_models_convert': SCIENCE,
    'portal_models_format_spec_get': ANY,
    'portal_models_get': ANY,
    'portal_models_import': SCIENCE,
    'portal_models_inspect': SCIENCE,
    'portal_models_list': ANY,
    'portal_models_manifest_transform': SCIENCE,
    'portal_models_package': SCIENCE,
    'portal_models_publish': SCIENCE,
    'portal_models_stage_set': SCIENCE,
    'portal_models_training_create': SCIENCE,
    'portal_models_training_get': ANY,
    'portal_models_training_list': ANY,
    'portal_models_training_logs_get': ANY,
    'portal_models_types_get': ANY,
    'portal_models_validate': SCIENCE,
    'portal_models_vllm_engine_configuration_set': SCIENCE,
    'portal_models_vllm_engine_spec_get': ANY,
    'portal_models_vllm_import': SCIENCE,
    'portal_workflows_build_branches_list': SCIENCE,
    'portal_workflows_build_cancel': SCIENCE,
    'portal_workflows_build_config_get': SCIENCE,
    'portal_workflows_build_config_set': PORTAL,
    'portal_workflows_build_get': SCIENCE,
    'portal_workflows_build_logs_get': SCIENCE,
    'portal_workflows_build_retry': SCIENCE,
    'portal_workflows_build_server_create': PORTAL,
    'portal_workflows_build_server_delete': PORTAL,
    'portal_workflows_build_server_start': PORTAL,
    'portal_workflows_build_server_stop': PORTAL,
    'portal_workflows_build_servers_list': SCIENCE,
    'portal_workflows_build_submit': SCIENCE,
    'portal_workflows_builds_list': SCIENCE,
    'portal_workflows_create': SCIENCE,
    'portal_workflows_custom_node_type_delete': ADMIN,
    'portal_workflows_custom_node_type_deprecate': ADMIN,
    'portal_workflows_custom_node_type_get': ANY,
    'portal_workflows_custom_node_type_register': ADMIN,
    'portal_workflows_custom_node_type_update': ADMIN,
    'portal_workflows_custom_node_types_list': ANY,
    'portal_workflows_delete': SCIENCE,
    'portal_workflows_duplicate': SCIENCE,
    'portal_workflows_generate': SCIENCE,
    'portal_workflows_generation_job_get': SCIENCE,
    'portal_workflows_get': VIEW,
    'portal_workflows_list': VIEW,
    'portal_workflows_node_catalog_get': VIEW,
    'portal_workflows_package': OPERATE,
    'portal_workflows_plugin_adjust_revision': ADMIN,
    'portal_workflows_plugin_architectures_set': ADMIN,
    'portal_workflows_plugin_build': ADMIN,
    'portal_workflows_plugin_builds_get': ANY,
    'portal_workflows_plugin_component_get': ANY,
    'portal_workflows_plugin_create': ADMIN,
    'portal_workflows_plugin_delete': ADMIN,
    'portal_workflows_plugin_demote': ADMIN,
    'portal_workflows_plugin_generate_message': ADMIN,
    'portal_workflows_plugin_generate_start': ADMIN,
    'portal_workflows_plugin_generate_status_get': ADMIN,
    'portal_workflows_plugin_get': ANY,
    'portal_workflows_plugin_gst_properties_get': ANY,
    'portal_workflows_plugin_import': ADMIN,
    'portal_workflows_plugin_modules_list': ANY,
    'portal_workflows_plugin_new_version': ADMIN,
    'portal_workflows_plugin_promote': ADMIN,
    'portal_workflows_plugin_review': PORTAL,
    'portal_workflows_plugin_select_plugins': ADMIN,
    'portal_workflows_plugin_simulate': ADMIN,
    'portal_workflows_plugin_source_get': ANY,
    'portal_workflows_plugin_source_set': ADMIN,
    'portal_workflows_plugin_update': ADMIN,
    'portal_workflows_plugin_version_get': VIEW,
    'portal_workflows_plugins_list': ANY,
    'portal_workflows_rename': SCIENCE,
    'portal_workflows_simulation_get': ANY,
    'portal_workflows_test_dataset_delete': SCIENCE,
    'portal_workflows_test_dataset_get': VIEW,
    'portal_workflows_test_datasets_list': VIEW,
    'portal_workflows_test_run_get': VIEW,
    'portal_workflows_test_run_start': SCIENCE,
    'portal_workflows_test_runs_list': VIEW,
    'portal_workflows_update': SCIENCE,
    'portal_workflows_validate': SCIENCE,
    'portal_workflows_versions_list': VIEW,
}


@pytest.fixture(scope='module')
def role_permissions(aws_stack):
    shared = sys.modules['shared_utils']
    return {role.value: {p.value for p in perms}
            for role, perms in shared.rbac_manager.role_permissions.items()}


def declared_roles(spec, role_permissions) -> set:
    return {role for role, perms in role_permissions.items()
            if all(p in perms for p in spec.permissions)}


def subset_problem(spec, role_permissions):
    extra = declared_roles(spec, role_permissions) - HANDLER_ROLES[spec.name]
    if extra:
        return f'{spec.name}: {spec.permissions} also admits {sorted(extra)}'
    return None


def test_every_routed_spec_has_a_recorded_handler_set():
    assert set(HANDLER_ROLES) == {s.name for s in ROUTED}


def test_recorded_sets_name_real_roles(role_permissions):
    for name, roles in HANDLER_ROLES.items():
        assert roles and roles <= set(role_permissions), name


def test_declared_permissions_exist(role_permissions):
    known = set().union(*role_permissions.values())
    for spec in ROUTED:
        assert spec.permissions, spec.name
        assert set(spec.permissions) <= known, spec.name


@pytest.mark.parametrize('spec', ROUTED, ids=lambda s: s.name)
def test_declared_roles_are_a_subset_of_the_handler_set(spec, role_permissions):
    assert subset_problem(spec, role_permissions) is None


# ------------------------------------------------- fixed regression cases

@pytest.mark.parametrize('name,declared,former,extra', [
    # review-7 H1: the handler's authorize_workflow_access(..., WORKFLOW_EDIT)
    ('portal_workflows_validate', 'workflow:edit', 'workflow:read', {'Viewer', 'Operator'}),
    # review-8 M1: the synthetic reads (synthetic_data._authorize, 'DataScientist')
    ('portal_data_synthetic_models_list', 'manage_datasets', 'view_datasets',
     {'Viewer', 'Operator'}),
    ('portal_data_synthetic_prompt_templates_get', 'manage_datasets', 'view_datasets',
     {'Viewer', 'Operator'}),
    ('portal_data_synthetic_sessions_list', 'manage_datasets', 'view_datasets',
     {'Viewer', 'Operator'}),
    ('portal_data_synthetic_session_get', 'manage_datasets', 'view_datasets',
     {'Viewer', 'Operator'}),
    # packaging / publish check 'DataScientist'
    ('portal_models_package', 'promote_models', 'manage_deployments', {'Operator'}),
    ('portal_models_publish', 'promote_models', 'manage_deployments', {'Operator'}),
    # _require_review_admin: UseCaseAdmin or PortalAdmin
    ('portal_data_labeling_review_get', 'update_usecases', 'manage_labeling_jobs',
     {'DataScientist'}),
    ('portal_data_labeling_review_decide', 'update_usecases', 'manage_labeling_jobs',
     {'DataScientist'}),
    ('portal_data_labeling_review_finalize', 'update_usecases', 'manage_labeling_jobs',
     {'DataScientist'}),
    # get_generation_job: workflow:create
    ('portal_workflows_generation_job_get', 'workflow:create', 'workflow:read',
     {'Viewer', 'Operator'}),
    # a global-row UseCaseAdmin under enforcement
    ('portal_admin_audit_logs_query', 'system_admin', 'view_audit_logs', {'UseCaseAdmin'}),
    # branch base: provision_components requires PortalAdmin on the Use_Case
    ('portal_devices_shared_components_provision', 'system_admin', 'update_usecases',
     {'UseCaseAdmin'}),
])
def test_former_permission_fails_with_the_extra_roles_named(
        name, declared, former, extra, role_permissions):
    spec = BY_NAME[name]
    assert spec.permissions == (declared,)
    assert subset_problem(spec, role_permissions) is None
    variant = dataclasses.replace(spec, permissions=(former,))
    problem = subset_problem(variant, role_permissions)
    assert problem is not None
    named = declared_roles(variant, role_permissions) - HANDLER_ROLES[name]
    assert extra <= named, problem
    for role in extra:
        assert role in problem


def test_models_delete_is_absent_while_the_handler_checks_a_role_that_does_not_exist():
    # models.py checks the literal 'Admin', which no Role carries: the
    # handler-admitted set is empty (Backlog 1).
    assert 'portal_models_delete' not in BY_NAME
    assert 'portal_models_delete' not in HANDLER_ROLES
