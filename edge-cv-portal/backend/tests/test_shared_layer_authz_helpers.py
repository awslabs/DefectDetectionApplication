"""
Unit tests for the shared-layer helpers of portal-authorization-and-iam-scoping
(tasks.md 2.1; design I1, I2, I5).

Covers, without AWS: the stdlib-only internal-invocation module and its
re-export from shared_utils; the account and role-ARN grammar; the
trusted-account check; the one Component_Bucket_Name derivation and
`portal_owned_buckets()` calling it under its own guard; the
Data_Plane_Scoping accessor; the tag-namespace validator; and the component
bucket policy size bound. The sibling's own `portal_owned_buckets` cases in
test_usecase_bucket_config_units.py stay unedited and keep passing.

Validates: Requirements 3.1, 3.3, 3.5, 4.1, 7.7, 7.11
"""
import ast
import os
import re
import sys

import pytest

import internal_invocation
import shared_utils as su

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_INTERNAL_INVOCATION = os.path.join(_BACKEND, "layers", "shared", "python", "internal_invocation.py")
_COMPUTE_STACK = os.path.abspath(os.path.join(
    _BACKEND, "..", "infrastructure", "lib", "compute-stack.ts"))

PORTAL = "111111111111"
USECASE = "222222222222"
DATA = "333333333333"
UNTRUSTED = "444444444444"


class Recorder:
    """Callable that records every call and delegates to `wraps`."""

    def __init__(self, wraps):
        self.calls = []
        self.wraps = wraps

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.wraps(*args, **kwargs)


# ---------------------------------------------------------------------------
# internal_invocation.py (Requirement 4.1, design I2)
# ---------------------------------------------------------------------------

def _gateway_context(**ids):
    return {"authorizer": {"claims": {"sub": "user-1"}}, **ids}


@pytest.mark.parametrize("event, expected", [
    ({"dda_internal": {"source": "s", "purpose": "p", "user_id": "system"}}, True),
    ({"dda_internal": {"source": "s"}, "requestContext": {}}, True),
    ({"dda_internal": {"source": "s"}, "requestContext": None}, True),
    ({"dda_internal": {"source": "s"}, "requestContext": _gateway_context()}, True),
    ({"dda_internal": {"source": "s"}, "requestContext": _gateway_context(requestId="r-1")}, False),
    ({"dda_internal": {"source": "s"}, "requestContext": _gateway_context(apiId="a-1")}, False),
    ({"dda_internal": {"source": "s"},
      "requestContext": _gateway_context(requestId="r-1", apiId="a-1")}, False),
    ({}, False),
    ({"requestContext": {}}, False),
    ({"dda_internal": "system"}, False),
    ({"dda_internal": ["source"]}, False),
    ({"dda_internal": None}, False),
    ({"body": '{"dda_internal": {"source": "s"}}', "requestContext": {}}, False),
])
def test_is_internal_invocation(event, expected):
    assert internal_invocation.is_internal_invocation(event) is expected


def test_internal_marker_shape_and_default_actor():
    assert internal_invocation.INTERNAL_MARKER_KEY == "dda_internal"
    assert internal_invocation.internal_marker("training_events", "auto_compile") == {
        "source": "training_events", "purpose": "auto_compile", "user_id": "system"}
    assert internal_invocation.internal_marker("model_import", "auto_compile", "user-7") == {
        "source": "model_import", "purpose": "auto_compile", "user_id": "user-7"}


def test_marker_built_by_the_helper_is_recognised():
    event = {internal_invocation.INTERNAL_MARKER_KEY: internal_invocation.internal_marker("a", "b")}
    assert internal_invocation.is_internal_invocation(event)


def test_shared_utils_re_exports_the_same_objects():
    assert su.INTERNAL_MARKER_KEY is internal_invocation.INTERNAL_MARKER_KEY
    assert su.internal_marker is internal_invocation.internal_marker
    assert su.is_internal_invocation is internal_invocation.is_internal_invocation


def _module_tree():
    with open(_INTERNAL_INVOCATION, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=_INTERNAL_INVOCATION)


def test_internal_invocation_imports_only_the_standard_library():
    imported = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            imported.add(node.module.split(".")[0])
    assert imported, "the module imports typing"
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert "boto3" not in imported and "shared_utils" not in imported


def test_internal_invocation_has_no_python_310_syntax():
    """The host lane and the audit script load the module on Python 3.9: no
    `match` statement and no `X | Y` union in an annotation."""
    tree = _module_tree()
    match_node = getattr(ast, "Match", None)
    for node in ast.walk(tree):
        if match_node is not None:
            assert not isinstance(node, match_node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations = [arg.annotation for arg in node.args.args + node.args.kwonlyargs]
            annotations.append(node.returns)
            for annotation in filter(None, annotations):
                for inner in ast.walk(annotation):
                    assert not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr))


# ---------------------------------------------------------------------------
# Account grammar (Requirement 3.1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (PORTAL, True),
    (USECASE, True),
    ("12345678901", False),            # eleven digits
    ("1234567890123", False),          # thirteen digits
    ("11111111111a", False),
    ("acct-remote", False),
    (PORTAL + "\n", False),            # `$` alone would accept the newline
    (" " + PORTAL, False),
    ("\u0661" * 12, False),            # non-ASCII digits
    (111111111111, False),             # not a string
    (None, False),
    ("", False),
])
def test_validate_account_id(value, expected):
    assert su.validate_account_id(value) is expected


ACCESS_ROLE = "DDAPortalAccessRole"
DATA_ROLE = "DDAPortalDataAccessRole"


@pytest.mark.parametrize("value, account_id, role_name, expected", [
    # pinned names: exactly arn:aws:iam::<account>:role/<name>
    (f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}", USECASE, ACCESS_ROLE, True),
    (f"arn:aws:iam::{DATA}:role/{DATA_ROLE}", DATA, DATA_ROLE, True),
    (f"arn:aws:iam::{USECASE}:role/OtherRole", USECASE, ACCESS_ROLE, False),
    (f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}x", USECASE, ACCESS_ROLE, False),
    (f"arn:aws:iam::{USECASE}:role/path/{ACCESS_ROLE}", USECASE, ACCESS_ROLE, False),
    (f"arn:aws:iam::{DATA}:role/{DATA_ROLE}", DATA, ACCESS_ROLE, False),
    (f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}", DATA, ACCESS_ROLE, False),     # account mismatch
    # generic grammar: any role name, account segment == account_id
    (f"arn:aws:iam::{USECASE}:role/service-role/SageMakerExecution", USECASE, None, True),
    (f"arn:aws:iam::{USECASE}:role/a+b=c,d.e@f_g-h", USECASE, None, True),
    (f"arn:aws:iam::{USECASE}:role/SageMakerExecution", PORTAL, None, False),      # account mismatch
    # malformed
    (f"arn:aws:iam::{USECASE}:root", USECASE, None, False),
    (f"arn:aws:iam::{USECASE}:user/someone", USECASE, None, False),
    (f"arn:aws:sts::{USECASE}:assumed-role/{ACCESS_ROLE}/s", USECASE, None, False),
    (f"arn:aws:iam::{USECASE}:role/", USECASE, None, False),
    (f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}\n", USECASE, ACCESS_ROLE, False),
    (f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}\n", USECASE, None, False),
    (f"arn:aws:iam::{USECASE}:role/bad name", USECASE, None, False),
    ("arn:aws:iam::acct-remote:role/" + ACCESS_ROLE, "acct-remote", ACCESS_ROLE, False),
    (None, USECASE, None, False),
    (f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}", None, ACCESS_ROLE, False),
])
def test_validate_role_arn(value, account_id, role_name, expected):
    assert su.validate_role_arn(value, account_id, role_name) is expected


def test_validate_role_arn_role_name_is_keyword_or_positional():
    arn = f"arn:aws:iam::{USECASE}:role/{ACCESS_ROLE}"
    assert su.validate_role_arn(arn, USECASE, role_name=ACCESS_ROLE)
    assert su.validate_role_arn(arn, USECASE)


def test_grammar_constants():
    assert su.ACCOUNT_ID_RE.pattern == r"^\d{12}$"
    assert su.ROLE_ARN_RE.pattern == r"^arn:aws:iam::(\d{12}):role/[\w+=,.@/-]+$"
    assert su.ACCOUNT_ID_RE.flags & re.ASCII and su.ROLE_ARN_RE.flags & re.ASCII


# ---------------------------------------------------------------------------
# Trusted accounts (Requirement 2.1)
# ---------------------------------------------------------------------------

def test_account_trusted_listed_and_unlisted(monkeypatch):
    monkeypatch.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"{USECASE},{DATA}")
    assert su.account_trusted(USECASE) is True
    assert su.account_trusted(DATA) is True
    assert su.account_trusted(UNTRUSTED) is False
    assert su.account_trusted(USECASE[:-1]) is False
    assert su.account_trusted(None) is False


def test_account_trusted_trims_entries_and_ignores_empty_ones(monkeypatch):
    monkeypatch.setenv("TRUSTED_USECASE_ACCOUNT_IDS", f"  {USECASE} ,, {DATA}  ,")
    assert su.account_trusted(USECASE) is True
    assert su.account_trusted(DATA) is True
    assert su.account_trusted(f" {USECASE}") is False
    assert su.account_trusted("") is False


@pytest.mark.parametrize("raw", [None, "", "   ", ",", " , ,"])
def test_account_trusted_unconfigured_raises(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("TRUSTED_USECASE_ACCOUNT_IDS", raising=False)
    else:
        monkeypatch.setenv("TRUSTED_USECASE_ACCOUNT_IDS", raw)
    with pytest.raises(su.TrustedAccountsNotConfigured) as info:
        su.account_trusted(USECASE)
    assert str(info.value) == "Trusted account list is not configured"


# ---------------------------------------------------------------------------
# Component_Bucket_Name (Requirement 3.5) and portal_owned_buckets
# ---------------------------------------------------------------------------

@pytest.fixture
def component_env(monkeypatch):
    for key in ("COMPONENT_BUCKET", "COMPONENT_BUCKET_PREFIX", "AWS_REGION",
                "PORTAL_ACCOUNT_ID", "PORTAL_ARTIFACTS_BUCKET"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_component_bucket_name_explicit_value_wins(component_env):
    component_env.setenv("COMPONENT_BUCKET", "explicit-component")
    component_env.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    component_env.setenv("AWS_REGION", "eu-west-1")
    assert su.component_bucket_name() == "explicit-component"


def test_component_bucket_name_derivation(component_env):
    component_env.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    component_env.setenv("AWS_REGION", "eu-west-1")
    assert su.component_bucket_name() == f"dda-component-eu-west-1-{PORTAL}"
    component_env.setenv("COMPONENT_BUCKET_PREFIX", "custom-prefix")
    assert su.component_bucket_name() == f"custom-prefix-eu-west-1-{PORTAL}"
    component_env.setenv("COMPONENT_BUCKET", "")     # empty counts as unset
    assert su.component_bucket_name() == f"custom-prefix-eu-west-1-{PORTAL}"


def test_component_bucket_name_region_unset_defaults_to_us_east_1(component_env):
    component_env.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    assert su.component_bucket_name() == f"dda-component-us-east-1-{PORTAL}"


def test_component_bucket_name_is_always_a_string(component_env):
    assert su.component_bucket_name() == "dda-component-us-east-1-"


def test_component_bucket_name_matches_the_usecases_role_grant(component_env):
    """The name compute-stack.ts grants the UseCases role
    `s3:ListBucket`/`GetBucketPolicy`/`PutBucketPolicy` on is
    `dda-component-${REGION}-${ACCOUNT_ID}`; for a given region and account
    the helper yields the same string."""
    with open(_COMPUTE_STACK, encoding="utf-8") as fh:
        source = fh.read()
    declaration = re.search(
        r"const componentBucketNameForUsecases = `(dda-component-\$\{cdk\.Aws\.REGION\}"
        r"-\$\{cdk\.Aws\.ACCOUNT_ID\})`;", source)
    assert declaration, "the UseCases component-bucket name template moved"
    assert "resources: [`arn:aws:s3:::${componentBucketNameForUsecases}`]" in source
    template = declaration.group(1)
    for region in ("us-east-1", "eu-west-1", "ap-southeast-2"):
        component_env.setenv("AWS_REGION", region)
        component_env.setenv("PORTAL_ACCOUNT_ID", PORTAL)
        granted = template.replace("${cdk.Aws.REGION}", region).replace("${cdk.Aws.ACCOUNT_ID}", PORTAL)
        assert su.component_bucket_name() == granted


def test_portal_owned_buckets_calls_the_helper_only_under_its_guard(component_env):
    recorder = Recorder(su.component_bucket_name)
    component_env.setattr(su, "component_bucket_name", recorder)
    component_env.setenv("PORTAL_ARTIFACTS_BUCKET", "arts")

    assert su.portal_owned_buckets() == frozenset({"arts"})
    component_env.setenv("PORTAL_ACCOUNT_ID", "")
    assert su.portal_owned_buckets() == frozenset({"arts"})
    assert recorder.calls == []

    component_env.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    # With the account set the owned set also names the workflow telemetry
    # lake (workflow-telemetry-dashboards design §7.5), derived from the same
    # environment; the component name still comes from the helper.
    assert su.portal_owned_buckets() == frozenset({
        "arts", f"dda-component-us-east-1-{PORTAL}", f"edgecv-portal-telemetry-{PORTAL}-us-east-1"})
    component_env.delenv("PORTAL_ACCOUNT_ID")
    component_env.setenv("COMPONENT_BUCKET", "comp-explicit")
    assert su.portal_owned_buckets() == frozenset({"arts", "comp-explicit"})
    assert len(recorder.calls) == 2


def test_portal_owned_buckets_names_the_helpers_result(component_env):
    component_env.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    component_env.setenv("AWS_REGION", "eu-west-1")
    component_env.setenv("COMPONENT_BUCKET_PREFIX", "custom-prefix")
    assert su.portal_owned_buckets() == frozenset({
        su.component_bucket_name(), f"edgecv-portal-telemetry-{PORTAL}-eu-west-1"})


# ---------------------------------------------------------------------------
# Data_Plane_Scoping accessor, tag namespace, policy bound
# ---------------------------------------------------------------------------

def test_data_plane_scoping_unset_is_tag(monkeypatch):
    monkeypatch.delenv("DATA_PLANE_SCOPING", raising=False)
    assert su.data_plane_scoping() == "tag"


@pytest.mark.parametrize("value", ["tag", "tag-objects-only", "allowlist"])
def test_data_plane_scoping_reads_the_variable(monkeypatch, value):
    monkeypatch.setenv("DATA_PLANE_SCOPING", value)
    assert su.data_plane_scoping() == value


def test_assert_portal_tags_returns_a_conforming_list_unchanged():
    tags = [{"Key": "dda-portal:managed", "Value": "true"},
            {"Key": "dda-portal:created-by", "Value": "portal"},
            {"Key": "dda-portal:usecase-id", "Value": "uc-1"}]
    snapshot = [dict(tag) for tag in tags]
    assert su.assert_portal_tags(tags) is tags
    assert tags == snapshot
    assert su.assert_portal_tags([]) == []


@pytest.mark.parametrize("bad", [
    {"Key": "Name", "Value": "x"},
    {"Key": "dda-portal-managed", "Value": "true"},
    {"Key": "DDA-PORTAL:managed", "Value": "true"},
    {"Key": " dda-portal:managed", "Value": "true"},
    {"Key": "aws:cloudformation:stack-name", "Value": "x"},
    {"Value": "no key"},
    {"Key": None, "Value": "x"},
    ("dda-portal:managed", "true"),
])
def test_assert_portal_tags_raises_outside_the_namespace(bad):
    tags = [{"Key": "dda-portal:managed", "Value": "true"}, bad]
    with pytest.raises(ValueError):
        su.assert_portal_tags(tags)


def test_component_bucket_policy_max_bytes():
    assert su.COMPONENT_BUCKET_POLICY_MAX_BYTES == 20_000
    assert su.COMPONENT_BUCKET_POLICY_MAX_BYTES < 20_480
