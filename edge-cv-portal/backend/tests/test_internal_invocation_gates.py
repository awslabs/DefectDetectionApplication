"""
Source-derived role checks and audit actors of compile, package and publish
(portal-authorization-and-iam-scoping, design I2 "Test strategy"; task 2.3).

The real handlers behind internal_gate_fakes: the role check is the portal's
own check_user_access over the moto user-roles table (run with
PORTAL_REGISTRY_ENFORCED off and on); everything after it is faked so an
admitted request reaches its audit row. The five internal-caller events are
the ones the real fabricators build (training_events, model_import,
compilation_events, packaging._trigger_component_creation,
detector_conversion.build_finalize_event).

Validates: Requirements 4.1-4.6
"""
import json

import pytest

import internal_gate_fakes as gf
from internal_invocation import INTERNAL_MARKER_KEY, internal_marker

COMPILE_BODY = {"targets": ["x86_64-cpu"]}
PUBLISH_BODY = {"component_name": "model-gate-model", "component_version": "1.0.0"}


@pytest.fixture(scope="module")
def gates(aws_stack):
    import shared_utils
    mp = pytest.MonkeyPatch()
    harness = gf.install(aws_stack, mp)
    harness.shared = shared_utils
    yield harness
    mp.undo()


@pytest.fixture(params=["enforcement-off", "enforcement-on"])
def env(request, gates, monkeypatch):
    if request.param == "enforcement-on":
        monkeypatch.setenv(gates.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
    else:
        monkeypatch.delenv(gates.shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    gates.reset()
    gates.ids = gates.seed_jobs()
    return gates


def compile_path(training_id):
    return f"/api/v1/training/{training_id}/compile"


def package_path(training_id):
    return f"/api/v1/training/{training_id}/package"


def publish_path(training_id):
    return f"/api/v1/training/{training_id}/publish"


def audit_row(env, action):
    [row] = env.audit.of(action)
    return row


# ----------------------------------------------------------------- compile

def test_gateway_auto_triggered_without_the_role_is_403_and_starts_nothing(env):
    caller = env.user("Operator")
    event = env.gateway_event(compile_path(env.ids["compile"]), env.ids["compile"], caller,
                              {**COMPILE_BODY, "auto_triggered": True})
    status, payload = env.compile(event)
    assert status == 403, payload
    assert env.sagemaker.calls == [] and env.audit.rows == []


def test_gateway_data_scientist_with_auto_triggered_is_audited_under_their_own_id(env):
    caller = env.user("DataScientist")
    event = env.gateway_event(compile_path(env.ids["compile"]), env.ids["compile"], caller,
                              {**COMPILE_BODY, "auto_triggered": True})
    status, payload = env.compile(event)
    assert status == 200, payload
    row = audit_row(env, "start_compilation")
    assert row["user_id"] == caller["user_id"]
    assert set(row["details"]) == {"targets", "compilation_jobs", "auto_triggered"}
    assert row["details"]["auto_triggered"] is True


def test_gateway_data_scientist_compile_is_unchanged(env):
    caller = env.user("DataScientist")
    status, payload = env.compile(env.gateway_event(
        compile_path(env.ids["compile"]), env.ids["compile"], caller, COMPILE_BODY))
    assert status == 200, payload
    assert len(env.sagemaker.calls) == 1
    row = audit_row(env, "start_compilation")
    assert row["user_id"] == caller["user_id"] and row["details"]["auto_triggered"] is False
    assert "internal_source" not in row["details"]


def test_training_events_compile_proceeds_and_is_attributed_to_system(env):
    event = env.training_events_event(env.ids["compile"])
    assert event[INTERNAL_MARKER_KEY] == internal_marker("training_events", "auto_compile")
    status, payload = env.compile(event)
    assert status == 200, payload
    assert len(env.sagemaker.calls) == 1
    row = audit_row(env, "start_compilation")
    assert row["user_id"] == "system"
    assert row["details"]["internal_source"] == "training_events"
    assert row["details"]["auto_triggered"] is True


def test_model_import_compile_proceeds_and_is_attributed_to_the_importer(env):
    importer = env.user("DataScientist")
    event = env.model_import_event(importer)
    assert event[INTERNAL_MARKER_KEY] == internal_marker(
        "model_import", "auto_compile", importer["user_id"])
    env.audit.rows.clear()
    status, payload = env.compile(event)
    assert status == 200, payload
    row = audit_row(env, "start_compilation")
    assert row["user_id"] == importer["user_id"]
    assert row["details"]["internal_source"] == "model_import"


def test_marker_on_a_gateway_event_is_treated_as_external(env):
    caller = {"user_id": "system", "email": "system@example.com", "username": "system",
              "role": "Viewer"}
    marker = {INTERNAL_MARKER_KEY: internal_marker("training_events", "auto_compile")}
    status, _ = env.compile(env.gateway_event(
        compile_path(env.ids["compile"]), env.ids["compile"], caller, COMPILE_BODY, **marker))
    assert status == 403 and env.sagemaker.calls == []


@pytest.mark.parametrize("handler", ["compile", "package", "publish"])
def test_system_claim_without_the_marker_is_an_ordinary_caller(env, handler):
    """A fabricated `system` claim with no marker (a caller missed in the
    inventory) gets no carve-out: it holds no role, so 403."""
    training_id = env.ids[handler]
    event = {
        "httpMethod": "POST", "path": {"compile": compile_path, "package": package_path,
                                       "publish": publish_path}[handler](training_id),
        "pathParameters": {"id": training_id},
        "body": json.dumps({"compile": COMPILE_BODY, "package": {"auto_triggered": True},
                            "publish": {**PUBLISH_BODY, "auto_triggered": True}}[handler]),
        "requestContext": {"authorizer": {"claims": {
            "sub": "system", "email": "system@example.com", "cognito:username": "system"}}},
    }
    status, _ = getattr(env, handler)(event)
    assert status == 403
    assert env.audit.rows == [] and env.sagemaker.calls == [] and env.greengrass.calls == []


# ----------------------------------------------------------------- package

def test_compilation_events_package_proceeds_and_is_attributed_to_system(env):
    event = env.compilation_events_event(env.ids["package"])
    assert event[INTERNAL_MARKER_KEY] == internal_marker("compilation_events", "auto_package")
    status, payload = env.package(event)
    assert status == 200, payload
    assert audit_row(env, "package_components")["user_id"] == "system"
    assert env.trigger_calls == [env.ids["package"]]          # the publish chain follows


def test_finalize_package_proceeds_and_is_attributed_to_system(env):
    event = env.finalize_event(env.ids["package"])
    assert event[INTERNAL_MARKER_KEY] == internal_marker("detector_conversion", "finalize")
    status, payload = env.package(event)
    assert status == 200, payload
    assert audit_row(env, "package_components")["user_id"] == "system"


def test_gateway_package_needs_the_role_and_is_audited_under_the_caller(env):
    status, _ = env.package(env.gateway_event(
        package_path(env.ids["package"]), env.ids["package"], env.user("Viewer"),
        {"auto_triggered": True}))
    assert status == 403 and env.audit.rows == []
    caller = env.user("DataScientist")
    status, payload = env.package(env.gateway_event(
        package_path(env.ids["package"]), env.ids["package"], caller, {}))
    assert status == 200, payload
    assert audit_row(env, "package_components")["user_id"] == caller["user_id"]


# ----------------------------------------------------------------- publish

def test_publish_chain_proceeds_and_is_attributed_to_system(env):
    event = env.publish_chain_event(env.ids["publish"])
    assert event[INTERNAL_MARKER_KEY] == internal_marker("packaging", "publish_chain")
    status, payload = env.publish(event)
    assert status == 200, payload
    assert len(env.greengrass.calls) == 1
    assert audit_row(env, "publish_greengrass_component")["user_id"] == "system"
    assert env.greengrass.calls[0]["tags"]["dda-portal:created-by"] == "system"


def test_publish_actor_is_the_markers_user(env):
    caller = env.user(None)
    event = env.publish_chain_event(env.ids["publish"])
    event[INTERNAL_MARKER_KEY] = internal_marker("packaging", "publish_chain", caller["user_id"])
    status, payload = env.publish(event)
    assert status == 200, payload
    assert audit_row(env, "publish_greengrass_component")["user_id"] == caller["user_id"]


def test_gateway_publish_needs_the_role_and_is_audited_under_the_caller(env):
    status, _ = env.publish(env.gateway_event(
        publish_path(env.ids["publish"]), env.ids["publish"], env.user("Operator"),
        {**PUBLISH_BODY, "auto_triggered": True}))
    assert status == 403 and env.greengrass.calls == [] and env.audit.rows == []
    caller = env.user("DataScientist")
    status, payload = env.publish(env.gateway_event(
        publish_path(env.ids["publish"]), env.ids["publish"], caller, PUBLISH_BODY))
    assert status == 200, payload
    assert audit_row(env, "publish_greengrass_component")["user_id"] == caller["user_id"]
