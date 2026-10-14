"""
RBAC and audit tests for the Node_Designer feature area
(custom-node-designer task 3.4).

Covers Requirements 10.3, 13.1, 13.2, 13.3, 13.4, and 13.5 with:

1. A parameterized role x action permission-resolution matrix over the
   ten Requirement-13 actions (create, generate, import, simulate,
   register, promote, demote, approve, update, remove) for every role
   (UseCaseAdmin, PortalAdmin, DataScientist, Operator, Viewer),
   exercised directly against `rbac_manager` / `Permission`. This layer
   covers actions whose HTTP handlers land in later tasks (4.x import,
   5.x generate, 8.x simulate, 9.x register/deprecate/remove).

2. A request-level matrix against the operations implemented today in
   plugin_records.py (create / update / promote / demote / approve /
   reject), asserting permitted roles succeed and denied roles receive
   the standard authorization error envelope (13.4).

3. Audit-log assertions: every implemented operation writes the
   existing AuditLog table with action, acting user, and timestamp
   (10.3, 13.5), and denials write an `unauthorized_access` entry.

Extension point: when the pending handlers exist, add entries to
REQUEST_OPERATIONS below (prepare / invoke / allowed_roles /
denied_permission / audit_action) and the request-level and audit
matrices pick them up automatically. The pending operations and their
expected owners are listed next to REQUEST_OPERATIONS.

Runs against the moto-backed stack from conftest.py, exercising the
real RBAC / audit / persistence code paths.
"""
import pytest

from test_plugin_records import PluginRecordsEnv


# --------------------------------------------------------------- fixtures

@pytest.fixture
def penv(aws_stack):
    return PluginRecordsEnv(aws_stack)


@pytest.fixture
def shared(aws_stack):
    """The real shared_utils module imported inside the moto mock.

    Imported lazily (after aws_stack re-imports it) so the Permission /
    Role enum classes match the ones rbac_manager was built with.
    """
    import shared_utils
    return shared_utils


def actor_with_role(penv, usecase_id, role_name):
    """A fresh user holding `role_name` for `usecase_id`.

    PortalAdmin arrives via the JWT custom:role claim (global role);
    every other role via a Use_Case assignment in the UserRoles table,
    mirroring production role resolution.
    """
    if role_name == "PortalAdmin":
        return penv.make_user(role="PortalAdmin")
    user = penv.make_user(role="Viewer")
    penv.assign_role(user, usecase_id, role_name)
    return user


# ----------------------------------------------------- role x action matrix

ROLES = ("UseCaseAdmin", "PortalAdmin", "DataScientist", "Operator", "Viewer")

# The ten Requirement-13 actions and the registered RBAC permission each
# one resolves to (task 3.3 / design "Access control and audit").
MATRIX_ACTIONS = {
    "create": "node-designer:create",
    "generate": "node-designer:generate",
    "import": "node-designer:import",
    "simulate": "node-designer:simulate",
    "register": "node-designer:register",
    "promote": "node-designer:promote-demote",
    "demote": "node-designer:promote-demote",
    "approve": "node-designer:security-review",
    "update": "node-designer:manage",
    "remove": "node-designer:manage",
}

# Expected grants per role: UseCaseAdmin holds the manage family within
# its own Use_Case (13.1); PortalAdmin holds everything including
# security review (13.1, 13.2); DataScientist / Operator / Viewer hold
# none of the mutating actions (13.3, 13.4).
ROLE_ALLOWED_ACTIONS = {
    "UseCaseAdmin": set(MATRIX_ACTIONS) - {"approve"},
    "PortalAdmin": set(MATRIX_ACTIONS),
    "DataScientist": set(),
    "Operator": set(),
    "Viewer": set(),
}


class TestPermissionResolutionMatrix:
    """Role x action resolution through rbac_manager (13.1-13.4).

    Runs directly against permission resolution so it covers actions
    whose HTTP handlers are not implemented yet.
    """

    @pytest.mark.parametrize("role_name", ROLES)
    @pytest.mark.parametrize("action", sorted(MATRIX_ACTIONS))
    def test_role_action_matrix(self, penv, shared, role_name, action):
        usecase_id = penv.create_usecase()
        actor = actor_with_role(penv, usecase_id, role_name)
        permission = shared.Permission(MATRIX_ACTIONS[action])
        expected = action in ROLE_ALLOWED_ACTIONS[role_name]
        granted = shared.rbac_manager.has_permission(
            actor["user_id"], usecase_id, permission, user_info=actor)
        assert granted is expected, (
            f"{role_name} {'should' if expected else 'should not'} "
            f"hold {permission.value} (action '{action}')"
        )

    @pytest.mark.parametrize("role_name", ROLES)
    def test_every_role_holds_read(self, penv, shared, role_name):
        """node-designer:read is granted to every role (13.3)."""
        usecase_id = penv.create_usecase()
        actor = actor_with_role(penv, usecase_id, role_name)
        assert shared.rbac_manager.has_permission(
            actor["user_id"], usecase_id,
            shared.Permission.NODE_DESIGNER_READ, user_info=actor)

    def test_usecase_admin_grants_do_not_cross_usecases(self, penv, shared):
        """UseCaseAdmin manage grants are scoped to the own Use_Case (13.1)."""
        usecase_a = penv.create_usecase("Use Case A")
        usecase_b = penv.create_usecase("Use Case B")
        admin_of_a = actor_with_role(penv, usecase_a, "UseCaseAdmin")
        for value in sorted({MATRIX_ACTIONS[a] for a in MATRIX_ACTIONS
                             if a != "approve"}):
            permission = shared.Permission(value)
            assert shared.rbac_manager.has_permission(
                admin_of_a["user_id"], usecase_a, permission,
                user_info=admin_of_a)
            assert not shared.rbac_manager.has_permission(
                admin_of_a["user_id"], usecase_b, permission,
                user_info=admin_of_a)


# ------------------------------------------- request-level operation registry

def _prepare_none(penv, usecase_id, admin):
    return {}


def _prepare_dev_record(penv, usecase_id, admin):
    status, body = penv.create_plugin(admin, usecase_id)
    assert status == 201
    return {"plugin_id": body["plugin"]["plugin_id"]}


def _prepare_dev_record_with_build(penv, usecase_id, admin):
    ctx = _prepare_dev_record(penv, usecase_id, admin)
    penv.seed_artifact(ctx["plugin_id"], 1)
    return ctx


def _prepare_test_record(penv, usecase_id, admin):
    ctx = _prepare_dev_record_with_build(penv, usecase_id, admin)
    assert penv.promote(admin, ctx["plugin_id"], 1)[0] == 200
    return ctx


# Operations whose handlers exist today (plugin_records.py, task 3.1).
# Pending operations and their owning handlers -- add an entry here when
# each handler lands so it joins the request-level and audit matrices:
#   generate  -> node_generator.py    (task 5.x)   audit: generate_plugin_scaffold
#   import    -> plugin_importer.py   (task 4.x)   audit: import_plugin
#   simulate  -> plugin_simulator.py  (task 8.x)   audit: simulate_plugin
#   register  -> custom_node_types.py (task 9.x)   audit: register_custom_node_type
#   deprecate -> custom_node_types.py (task 9.x)   audit: deprecate_custom_node_type
#   remove    -> custom_node_types.py (task 9.x)   audit: remove_custom_node_type
REQUEST_OPERATIONS = {
    "create": dict(
        audit_action="create_plugin_record",
        success_status=201,
        prepare=_prepare_none,
        invoke=lambda penv, actor, usecase_id, ctx:
            penv.create_plugin(actor, usecase_id),
        allowed_roles={"UseCaseAdmin", "PortalAdmin"},
        denied_permission="node-designer:create",
    ),
    "update": dict(
        audit_action="update_plugin_record",
        success_status=200,
        prepare=_prepare_dev_record,
        invoke=lambda penv, actor, usecase_id, ctx:
            penv.invoke("PUT", "/plugins/{id}", actor, ctx["plugin_id"],
                        body={"description": "updated by rbac matrix"}),
        allowed_roles={"UseCaseAdmin", "PortalAdmin"},
        denied_permission="node-designer:manage",
    ),
    "promote": dict(
        audit_action="promote_plugin_record",
        success_status=200,
        prepare=_prepare_dev_record_with_build,
        invoke=lambda penv, actor, usecase_id, ctx:
            penv.promote(actor, ctx["plugin_id"], 1),
        allowed_roles={"UseCaseAdmin", "PortalAdmin"},
        denied_permission="node-designer:promote-demote",
    ),
    "demote": dict(
        audit_action="demote_plugin_record",
        success_status=200,
        prepare=_prepare_test_record,
        invoke=lambda penv, actor, usecase_id, ctx:
            penv.demote(actor, ctx["plugin_id"], 1),
        allowed_roles={"UseCaseAdmin", "PortalAdmin"},
        denied_permission="node-designer:promote-demote",
    ),
    "approve": dict(
        audit_action="security_review_approved",
        success_status=200,
        prepare=_prepare_dev_record,
        invoke=lambda penv, actor, usecase_id, ctx:
            penv.review(actor, ctx["plugin_id"], 1, "approved"),
        allowed_roles={"PortalAdmin"},
        denied_permission="node-designer:security-review",
    ),
    "reject": dict(
        audit_action="security_review_rejected",
        success_status=200,
        prepare=_prepare_dev_record,
        invoke=lambda penv, actor, usecase_id, ctx:
            penv.review(actor, ctx["plugin_id"], 1, "rejected"),
        allowed_roles={"PortalAdmin"},
        denied_permission="node-designer:security-review",
    ),
}


# --------------------------------------------- request-level RBAC matrix

class TestRequestLevelMatrix:
    """Role x operation over the live plugin_records endpoints.

    Permitted roles complete the operation (13.1, 13.2); every other
    role is denied with the standard authorization error envelope
    (13.4).
    """

    @pytest.mark.parametrize("role_name", ROLES)
    @pytest.mark.parametrize("op_name", sorted(REQUEST_OPERATIONS))
    def test_role_operation(self, penv, op_name, role_name):
        op = REQUEST_OPERATIONS[op_name]
        usecase_id = penv.create_usecase()
        setup_admin = actor_with_role(penv, usecase_id, "UseCaseAdmin")
        ctx = op["prepare"](penv, usecase_id, setup_admin)
        actor = actor_with_role(penv, usecase_id, role_name)

        status, body = op["invoke"](penv, actor, usecase_id, ctx)

        if role_name in op["allowed_roles"]:
            assert status == op["success_status"], (
                f"{role_name} should be permitted to {op_name}: {body}")
        else:
            # Standard authorization error envelope (13.4)
            assert status == 403, (
                f"{role_name} should be denied {op_name}, got {status}: {body}")
            assert body["error"]["code"] == "FORBIDDEN"
            assert body["error"]["message"] == "Insufficient permissions"
            assert body["error"]["details"]["required_permissions"] == [
                op["denied_permission"]]

    @pytest.mark.parametrize("op_name",
                             sorted(set(REQUEST_OPERATIONS) - {"create"}))
    def test_usecase_admin_of_other_usecase_denied(self, penv, op_name):
        """A UseCaseAdmin of a different Use_Case cannot manage this
        Use_Case's Plugin_Records (13.1: own Use_Case only)."""
        op = REQUEST_OPERATIONS[op_name]
        usecase_id = penv.create_usecase("Owning Use Case")
        setup_admin = actor_with_role(penv, usecase_id, "UseCaseAdmin")
        ctx = op["prepare"](penv, usecase_id, setup_admin)

        other_usecase = penv.create_usecase("Other Use Case")
        outsider = actor_with_role(penv, other_usecase, "UseCaseAdmin")

        status, body = op["invoke"](penv, outsider, usecase_id, ctx)
        assert status in (403, 404), (
            f"Foreign UseCaseAdmin must not {op_name}, got {status}: {body}")

    @pytest.mark.parametrize("role_name", ROLES)
    def test_every_role_reads_plugin_records(self, penv, role_name):
        """All five roles view Plugin_Records read-only (13.3)."""
        usecase_id = penv.create_usecase()
        setup_admin = actor_with_role(penv, usecase_id, "UseCaseAdmin")
        ctx = _prepare_dev_record(penv, usecase_id, setup_admin)
        actor = actor_with_role(penv, usecase_id, role_name)

        status, body = penv.invoke("GET", "/plugins", actor,
                                   query={"usecase_id": usecase_id})
        assert status == 200
        assert ctx["plugin_id"] in [p["plugin_id"] for p in body["plugins"]]

        status, body = penv.invoke("GET", "/plugins/{id}", actor,
                                   ctx["plugin_id"])
        assert status == 200
        assert body["plugin"]["plugin_id"] == ctx["plugin_id"]


# ------------------------------------------------- audit writes (10.3, 13.5)

class TestAuditWrites:
    """Every operation writes the existing AuditLog table with the
    action, the acting user, and a timestamp (10.3, 13.5)."""

    @pytest.mark.parametrize("op_name", sorted(REQUEST_OPERATIONS))
    def test_operation_writes_audit_entry(self, penv, op_name):
        op = REQUEST_OPERATIONS[op_name]
        usecase_id = penv.create_usecase()
        setup_admin = actor_with_role(penv, usecase_id, "UseCaseAdmin")
        ctx = op["prepare"](penv, usecase_id, setup_admin)
        role = "PortalAdmin" if op["allowed_roles"] == {"PortalAdmin"} \
            else "UseCaseAdmin"
        actor = actor_with_role(penv, usecase_id, role)

        status, body = op["invoke"](penv, actor, usecase_id, ctx)
        assert status == op["success_status"], body

        resource_id = ctx.get("plugin_id") or body["plugin"]["plugin_id"]
        entries = [e for e in penv.audit_entries(op["audit_action"])
                   if e["resource_id"] == resource_id
                   and e["user_id"] == actor["user_id"]]
        assert len(entries) == 1, (
            f"expected exactly one '{op['audit_action']}' audit entry "
            f"for {resource_id} by {actor['user_id']}")
        entry = entries[0]
        # Action, acting user, timestamp (13.5)
        assert entry["action"] == op["audit_action"]
        assert entry["user_id"] == actor["user_id"]
        assert entry["timestamp"] > 0
        assert entry["result"] == "success"
        assert entry["details"]["usecase_id"] == usecase_id

    def test_new_version_update_writes_audit_entry(self, penv):
        """Creating a new version via PUT (the 'update' flavor that
        versions the record) is audited (13.5)."""
        usecase_id = penv.create_usecase()
        admin = actor_with_role(penv, usecase_id, "UseCaseAdmin")
        ctx = _prepare_dev_record(penv, usecase_id, admin)

        status, _ = penv.invoke("PUT", "/plugins/{id}", admin,
                                ctx["plugin_id"], body={"new_version": True})
        assert status == 201

        entries = [e for e in penv.audit_entries("create_plugin_record_version")
                   if e["resource_id"] == ctx["plugin_id"]]
        assert len(entries) == 1
        assert entries[0]["user_id"] == admin["user_id"]
        assert entries[0]["timestamp"] > 0

    @pytest.mark.parametrize("op_name", sorted(REQUEST_OPERATIONS))
    def test_denied_operation_writes_unauthorized_access_entry(self, penv, op_name):
        """RBAC denials are themselves audited as unauthorized_access."""
        op = REQUEST_OPERATIONS[op_name]
        usecase_id = penv.create_usecase()
        setup_admin = actor_with_role(penv, usecase_id, "UseCaseAdmin")
        ctx = op["prepare"](penv, usecase_id, setup_admin)
        denied_role = "Viewer" if "UseCaseAdmin" in op["allowed_roles"] \
            else "UseCaseAdmin"
        actor = actor_with_role(penv, usecase_id, denied_role)

        status, _ = op["invoke"](penv, actor, usecase_id, ctx)
        assert status == 403

        entries = [e for e in penv.audit_entries("unauthorized_access")
                   if e["user_id"] == actor["user_id"]]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "denied"
        assert entry["timestamp"] > 0
        assert entry["details"]["required_permissions"] == [
            op["denied_permission"]]
