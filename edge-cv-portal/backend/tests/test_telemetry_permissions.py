"""The dashboards and telemetry permissions of ``shared_utils``
(workflow-telemetry-dashboards design §11.1, §2.4; tasks.md 2.1).

- the role matrix row by row through ``rbac_manager.has_permission`` under
  both enforcement states (a provisioned principal: an enabled global row and
  an enabled use-case row naming the role);
- under enforcement, a global Viewer row grants ``dashboards:read`` on every
  use case (N10), and a principal without a row gets nothing;
- ``portal_owned_buckets()`` holds the artifacts bucket, the derived component
  bucket and the derived lake bucket under the §2.4 environment.

The matrix below is the one table of §11.1. The frontend commits the same
table as ``src/utils/telemetryPermissionsMatrix.json`` (tasks.md 5.1); when
that file exists it must equal this one.
"""
import json
import os
import uuid

import pytest

# role -> (dashboards:read, dashboards:manage, telemetry:manage)
MATRIX = {
    "Viewer": {"dashboards:read": True, "dashboards:manage": False, "telemetry:manage": False},
    "Operator": {"dashboards:read": True, "dashboards:manage": True, "telemetry:manage": False},
    "DataScientist": {"dashboards:read": True, "dashboards:manage": True, "telemetry:manage": False},
    "UseCaseAdmin": {"dashboards:read": True, "dashboards:manage": True, "telemetry:manage": True},
    "PortalAdmin": {"dashboards:read": True, "dashboards:manage": True, "telemetry:manage": True},
    "DataLabeler": {"dashboards:read": False, "dashboards:manage": False, "telemetry:manage": False},
}
PERMISSION_ATTRIBUTES = {
    "dashboards:read": "DASHBOARDS_READ",
    "dashboards:manage": "DASHBOARDS_MANAGE",
    "telemetry:manage": "TELEMETRY_MANAGE",
}
FRONTEND_MATRIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend", "src",
                               "utils", "telemetryPermissionsMatrix.json")


@pytest.fixture
def su(aws_stack):
    """The real shared layer as the moto stack re-imported it (a module-level
    import would bind the DynamoDB resource before the mock is active)."""
    import shared_utils
    return shared_utils


def _permission(su, name):
    return getattr(su.Permission, PERMISSION_ATTRIBUTES[name])


def test_permission_values(su):
    Permission = su.Permission
    assert Permission.DASHBOARDS_READ.value == "dashboards:read"
    assert Permission.DASHBOARDS_MANAGE.value == "dashboards:manage"
    assert Permission.TELEMETRY_MANAGE.value == "telemetry:manage"
    assert len({p.value for p in Permission}) == len(list(Permission)), "permission values are unique"


@pytest.mark.parametrize("role_name", sorted(MATRIX))
def test_static_role_matrix(su, role_name):
    """The RBACManager table itself, independent of any registry row."""
    granted = su.rbac_manager.role_permissions[su.Role(role_name)]
    for permission_name, expected in MATRIX[role_name].items():
        assert (_permission(su, permission_name) in granted) is expected, (role_name, permission_name)


def test_portal_admin_keeps_every_permission(su):
    assert su.rbac_manager.role_permissions[su.Role.PORTAL_ADMIN] == set(su.Permission)


def test_data_labeler_holds_only_its_labeler_permission(su):
    assert su.rbac_manager.role_permissions[su.Role.DATA_LABELER] == {su.Permission.LABELING_TASKS_SELF}


@pytest.fixture(params=["off", "on"])
def enforcement(request, monkeypatch):
    if request.param == "on":
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    else:
        monkeypatch.delenv("PORTAL_REGISTRY_ENFORCED", raising=False)
    return request.param


def _provision(env, role_name, usecase_id):
    user = env.make_user(role_name)
    for scope in ("global", usecase_id):
        env.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": scope, "role": role_name,
            "username": user["username"], "email": user["email"], "status": "enabled"})
    return user


@pytest.mark.parametrize("role_name", sorted(MATRIX))
def test_matrix_row_through_has_permission(env, su, enforcement, role_name):
    usecase_id = env.create_usecase()
    user = _provision(env, role_name, usecase_id)
    for permission_name, expected in MATRIX[role_name].items():
        decided = su.rbac_manager.has_permission(user["user_id"], usecase_id, _permission(su, permission_name), user)
        assert decided is expected, (enforcement, role_name, permission_name)


def test_global_viewer_row_reads_dashboards_on_every_use_case_under_enforcement(env, su, monkeypatch):
    monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    rbac_manager, Permission = su.rbac_manager, su.Permission
    user = env.make_user("Viewer")
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user["user_id"], "usecase_id": "global", "role": "Viewer", "status": "enabled"})
    for _ in range(3):
        usecase_id = env.create_usecase()
        assert rbac_manager.has_permission(user["user_id"], usecase_id, Permission.DASHBOARDS_READ, user) is True
        assert rbac_manager.has_permission(user["user_id"], usecase_id, Permission.DASHBOARDS_MANAGE, user) is False
        assert rbac_manager.has_permission(user["user_id"], usecase_id, Permission.TELEMETRY_MANAGE, user) is False


def test_use_case_row_overrides_the_global_row_under_enforcement(env, su, monkeypatch):
    monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    rbac_manager, Permission = su.rbac_manager, su.Permission
    usecase_id = env.create_usecase()
    other = env.create_usecase()
    user = env.make_user("Viewer")
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user["user_id"], "usecase_id": "global", "role": "Viewer", "status": "enabled"})
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user["user_id"], "usecase_id": usecase_id, "role": "UseCaseAdmin", "status": "enabled"})
    assert rbac_manager.has_permission(user["user_id"], usecase_id, Permission.TELEMETRY_MANAGE, user) is True
    assert rbac_manager.has_permission(user["user_id"], other, Permission.TELEMETRY_MANAGE, user) is False
    assert rbac_manager.has_permission(user["user_id"], other, Permission.DASHBOARDS_READ, user) is True


def test_unprovisioned_principal_gets_nothing_under_enforcement(env, su, monkeypatch):
    monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    usecase_id = env.create_usecase()
    user = env.make_user("PortalAdmin")  # the claim alone grants nothing
    for name in PERMISSION_ATTRIBUTES:
        assert su.rbac_manager.has_permission(user["user_id"], usecase_id, _permission(su, name), user) is False


def test_claim_alone_still_decides_while_enforcement_is_off(env, su, monkeypatch):
    monkeypatch.delenv("PORTAL_REGISTRY_ENFORCED", raising=False)
    rbac_manager, Permission = su.rbac_manager, su.Permission
    usecase_id = env.create_usecase()
    viewer = env.make_user("Viewer")
    assert rbac_manager.has_permission(viewer["user_id"], usecase_id, Permission.DASHBOARDS_READ, viewer) is True
    assert rbac_manager.has_permission(viewer["user_id"], usecase_id, Permission.DASHBOARDS_MANAGE, viewer) is False
    labeler = env.make_user("DataLabeler")
    assert rbac_manager.has_permission(labeler["user_id"], usecase_id, Permission.DASHBOARDS_READ, labeler) is False


@pytest.mark.skipif(not os.path.exists(FRONTEND_MATRIX), reason="the frontend matrix file is written by tasks.md 5.1")
def test_frontend_matrix_file_equals_the_backend_matrix():
    with open(FRONTEND_MATRIX, encoding="utf-8") as fh:
        shared = json.load(fh)
    assert shared == MATRIX


class TestPortalOwnedBuckets:
    ENV = {
        "PORTAL_ARTIFACTS_BUCKET": "edgecv-portal-artifacts-fixture",
        "COMPONENT_BUCKET_PREFIX": "dda-component",
        "PORTAL_ACCOUNT_ID": "111111111111",
        "AWS_REGION": "us-east-1",
    }

    def test_holds_the_three_buckets_under_the_lambda_environment(self, su, monkeypatch):
        shared_utils = su
        for key, value in self.ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
        assert shared_utils.portal_owned_buckets() == frozenset({
            "edgecv-portal-artifacts-fixture",
            "dda-component-us-east-1-111111111111",
            "edgecv-portal-telemetry-111111111111-us-east-1",
        })

    def test_lake_bucket_follows_the_region(self, su, monkeypatch):
        shared_utils = su
        for key, value in self.ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
        owned = shared_utils.portal_owned_buckets()
        assert "edgecv-portal-telemetry-111111111111-eu-west-1" in owned
        assert "dda-component-eu-west-1-111111111111" in owned

    def test_no_lake_bucket_without_the_portal_account(self, su, monkeypatch):
        shared_utils = su
        monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", "edgecv-portal-artifacts-fixture")
        monkeypatch.delenv("PORTAL_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
        assert shared_utils.portal_owned_buckets() == frozenset({"edgecv-portal-artifacts-fixture"})

    def test_lake_bucket_is_never_an_allowed_bucket(self, su, monkeypatch):
        shared_utils = su
        for key, value in self.ENV.items():
            monkeypatch.setenv(key, value)
        usecase = {"usecase_id": "uc-{0}".format(uuid.uuid4()),
                   "additional_s3_buckets": ["edgecv-portal-telemetry-111111111111-us-east-1", "customer-data"]}
        assert shared_utils.allowed_s3_buckets(usecase) == frozenset({"customer-data"})
