"""Property test for Plugin_Component version immutability (task 6.5).

**Feature: custom-node-designer, Property 23: Plugin_Component versions are immutable under rebuild**

For all sequences of source-change and rebuild operations on a plugin
(each round creating a new Plugin_Record version, recording per-arch
build results, and auto-packaging, with registration randomly failing),
every publish produces a Plugin_Component version not previously
registered, and the recipes and artifact references (and the account-
bucket artifact bytes) of all previously published Plugin_Component
versions are unchanged after each publish; component versions map 1:1
to Plugin_Record versions. Failed registration rounds clean up only
their own version and never touch previously published versions.

**Validates: Requirements 16.7**

Runs the real plugin_records + plugin_components handlers against the
moto-backed stack from conftest.py with a stateful fake Use_Case-account
Greengrass registry (moto does not implement greengrassv2). The fake
registry records every created component version so the test can assert
previously registered versions are never modified or deleted.
"""
import copy
import hashlib
import json
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import TEST_ENV

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")


# ---------------------------------------------------------------------------
# Stateful fake Greengrass registry (moto lacks greengrassv2)
# ---------------------------------------------------------------------------

class FakeGreengrassRegistry:
    """Records created component versions; a component version that
    already exists raises ConflictException, exactly like the real
    registry. `fail_next_registration` makes the next created version
    settle in BROKEN instead of DEPLOYABLE, driving the packaging
    failure/cleanup path."""

    def __init__(self, account_id="123456789012", region="us-east-1"):
        self.account_id = account_id
        self.meta = SimpleNamespace(region_name=region)
        self.versions = {}  # arn -> {"recipe": dict, "tags": dict, "state": str}
        self.fail_next_registration = False

    def _arn(self, recipe):
        return (f"arn:aws:greengrass:{self.meta.region_name}:{self.account_id}:"
                f"components:{recipe['ComponentName']}:versions:"
                f"{recipe['ComponentVersion']}")

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        arn = self._arn(recipe)
        if arn in self.versions:
            raise ClientError(
                {"Error": {"Code": "ConflictException",
                           "Message": "component version already exists"}},
                "CreateComponentVersion")
        state = "BROKEN" if self.fail_next_registration else "DEPLOYABLE"
        self.fail_next_registration = False
        self.versions[arn] = {"recipe": recipe, "tags": dict(tags or {}),
                              "state": state}
        return {"arn": arn}

    def describe_component(self, arn):
        version = self.versions[arn]
        return {"status": {"componentState": version["state"],
                           "message": "simulated"}}

    def delete_component(self, arn):
        self.versions.pop(arn, None)


# ---------------------------------------------------------------------------
# Module-scoped environment (hypothesis-safe: no function-scoped fixtures)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cenv(aws_stack):
    """plugin_components imported inside the moto mock, one Use_Case with
    an account bucket, and get_usecase_client patched to a swappable fake
    Greengrass registry (holder['gg']) plus the moto S3 client."""
    for name in ("plugin_components", "workflow_packaging"):
        sys.modules.pop(name, None)
    import plugin_components

    mp = pytest.MonkeyPatch()
    mp.setattr(plugin_components, "COMPONENT_STATUS_POLL_SECONDS", 0)

    holder = {"gg": None}
    moto_s3 = aws_stack.s3

    def fake_get_usecase_client(service_name, usecase, session_name=None,
                                region=None):
        return {"s3": moto_s3, "greengrassv2": holder["gg"]}[service_name]

    mp.setattr(plugin_components, "get_usecase_client",
               fake_get_usecase_client)

    usecase_id = f"uc-{uuid.uuid4()}"
    usecase_bucket = f"usecase-bucket-{uuid.uuid4()}"
    moto_s3.create_bucket(Bucket=usecase_bucket)
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Immutability Property Use Case",
        "account_id": "123456789012",
        "s3_bucket": usecase_bucket,
    })
    admin_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": admin_id, "usecase_id": usecase_id, "role": "UseCaseAdmin",
    })
    admin = {"user_id": admin_id, "email": f"{admin_id}@example.com",
             "username": admin_id, "role": "UseCaseAdmin"}

    yield SimpleNamespace(
        stack=aws_stack,
        module=plugin_components,
        records=aws_stack.plugin_records,
        s3=moto_s3,
        portal_bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
        usecase_id=usecase_id,
        usecase_bucket=usecase_bucket,
        admin=admin,
        holder=holder,
    )
    mp.undo()


# ---------------------------------------------------------------------------
# Handler invocation helpers (real plugin_records API events)
# ---------------------------------------------------------------------------

def _claims(user):
    return {"authorizer": {"claims": {
        "sub": user["user_id"],
        "email": user["email"],
        "cognito:username": user["username"],
        "custom:role": user["role"],
    }}}


def _create_plugin(cenv, name):
    """POST /plugins -> Plugin_Record version 1 (a plugin's first source)."""
    response = cenv.records.handler({
        "httpMethod": "POST", "resource": "/plugins", "path": "/plugins",
        "pathParameters": None, "queryStringParameters": None,
        "body": json.dumps({"usecase_id": cenv.usecase_id,
                            "name": name, "kind": "scaffold"}),
        "requestContext": _claims(cenv.admin),
    }, None)
    assert response["statusCode"] == 201, response["body"]
    return json.loads(response["body"])["plugin"]


def _new_version(cenv, plugin_id):
    """PUT /plugins/{id} new_version=true -> the rebuild/source-change
    path: a fresh Plugin_Record version (design: rebuilds always create
    a new Plugin_Record version, which packages as a new component
    version)."""
    response = cenv.records.handler({
        "httpMethod": "PUT", "resource": "/plugins/{id}",
        "path": f"/plugins/{plugin_id}",
        "pathParameters": {"id": plugin_id}, "queryStringParameters": None,
        "body": json.dumps({"new_version": True}),
        "requestContext": _claims(cenv.admin),
    }, None)
    assert response["statusCode"] == 201, response["body"]
    return json.loads(response["body"])["plugin"]


def _record_builds(cenv, plugin_id, version, name, built_archs):
    """Record successful per-arch builds: the rebuilt .so overwrites the
    portal Plugin_Library key (same key every round - the library holds
    the latest build), and the artifact entries land on the record."""
    artifacts = {}
    for arch in built_archs:
        data = f"\x7fELF {name} {arch} v{version} {uuid.uuid4()}".encode()
        key = f"workflow-plugins/custom/{cenv.usecase_id}/{arch}/{name}.so"
        cenv.s3.put_object(Bucket=cenv.portal_bucket, Key=key, Body=data)
        artifacts[arch] = {
            "buildStatus": "succeeded", "s3Key": key,
            "checksum": hashlib.sha256(data).hexdigest(),
            "signature": "c2ln", "logTail": "",
        }
    cenv.stack.tables.plugin_records.update_item(
        Key={"plugin_id": plugin_id, "version": version},
        UpdateExpression="SET artifacts = :a, requested_architectures = :r",
        ExpressionAttributeValues={":a": artifacts,
                                   ":r": sorted(built_archs)},
    )


def _package(cenv, plugin_id, version):
    return cenv.module.handler({
        "action": "package_plugin_component",
        "plugin_id": plugin_id, "version": version,
        "usecase_id": cenv.usecase_id,
    }, None)


def _account_objects(cenv, prefix):
    """{key: bytes} of every account-bucket object under a prefix."""
    listed = cenv.s3.list_objects_v2(Bucket=cenv.usecase_bucket,
                                     Prefix=prefix)
    return {
        obj["Key"]: cenv.s3.get_object(
            Bucket=cenv.usecase_bucket, Key=obj["Key"])["Body"].read()
        for obj in listed.get("Contents", [])
    }


# ---------------------------------------------------------------------------
# Rounds: each is one source-change/rebuild with random built archs and
# random registration success/failure.
# ---------------------------------------------------------------------------

rounds = st.lists(
    st.tuples(
        st.sets(st.sampled_from(ARCHS), min_size=1, max_size=3),
        st.booleans(),  # registration succeeds?
    ),
    min_size=1,
    max_size=4,
)


@settings(max_examples=25, deadline=None)
@given(build_rounds=rounds)
def test_component_versions_immutable_under_rebuild(cenv, build_rounds):
    """**Feature: custom-node-designer, Property 23: Plugin_Component versions are immutable under rebuild**

    For all sequences of source-change and rebuild operations on a
    plugin, every publish produces a Plugin_Component version not
    previously registered, and the recipes and artifact references of
    all previously published Plugin_Component versions are unchanged
    after each publish.

    **Validates: Requirements 16.7**
    """
    registry = FakeGreengrassRegistry()
    cenv.holder["gg"] = registry

    name = f"plg-{uuid.uuid4().hex[:12]}"
    plugin = _create_plugin(cenv, name)
    plugin_id = plugin["plugin_id"]

    # comp_version -> {"registry": deep copy of the registry entry,
    #                  "objects": {account key: bytes}} at publish time
    published = {}
    # plugin version -> comp version, for the 1:1 mapping check
    version_map = {}

    for round_index, (built_archs, registration_ok) in enumerate(build_rounds):
        if round_index == 0:
            version = plugin["version"]
        else:
            # Rebuild/source change -> always a new Plugin_Record version.
            version = _new_version(cenv, plugin_id)["version"]

        _record_builds(cenv, plugin_id, version, name, built_archs)
        registry.fail_next_registration = not registration_ok

        comp_version = cenv.module.component_version_for(version)
        arn = cenv.module.component_version_arn(
            "us-east-1", "123456789012", plugin_id, version)

        # A rebuild packages as a version not previously registered.
        assert comp_version not in published
        assert arn not in registry.versions

        result = _package(cenv, plugin_id, version)

        version_prefix = f"plugins/components/{plugin_id}/{version}/"
        if registration_ok:
            assert result["packaged"] is True, result
            assert result["component_version"] == comp_version
            # Registered in the registry exactly once, never before.
            assert arn in registry.versions
            published[comp_version] = {
                "registry": copy.deepcopy(registry.versions[arn]),
                "objects": _account_objects(cenv, version_prefix),
            }
            version_map[version] = comp_version
            # The publish shipped one artifact prefix per built arch.
            assert set(published[comp_version]["objects"]) == {
                f"{version_prefix}{arch}/{fname}"
                for arch in built_archs
                for fname in (f"{name}.so", "plugin-manifest.json")
            }
        else:
            # Failed registration cleans up only this version: nothing
            # registered, no artifacts under this version's prefix.
            assert result["packaged"] is False, result
            assert arn not in registry.versions
            assert _account_objects(cenv, version_prefix) == {}

        # Immutability (16.7): every previously published component
        # version's registry entry (recipe + tags + state) and account-
        # bucket artifacts are byte-identical after this publish.
        for prior_comp_version, snapshot in published.items():
            if prior_comp_version == comp_version and registration_ok:
                continue  # the version published this round
            prior_version = int(prior_comp_version.split(".")[0])
            prior_arn = cenv.module.component_version_arn(
                "us-east-1", "123456789012", plugin_id, prior_version)
            assert registry.versions[prior_arn] == snapshot["registry"]
            prior_prefix = f"plugins/components/{plugin_id}/{prior_version}/"
            assert _account_objects(cenv, prior_prefix) == snapshot["objects"]

        # No staging leftovers accumulate across rounds.
        assert _account_objects(cenv, f"plugins/staging/{plugin_id}/") == {}

    # Component versions map 1:1 to Plugin_Record versions: exactly the
    # successfully packaged record versions are registered, each as its
    # own distinct {version}.0.0 component version.
    expected_arns = {
        cenv.module.component_version_arn(
            "us-east-1", "123456789012", plugin_id, v)
        for v in version_map
    }
    assert set(registry.versions) == expected_arns
    assert len(set(version_map.values())) == len(version_map)
    for record_version, comp_version in version_map.items():
        assert comp_version == f"{record_version}.0.0"
