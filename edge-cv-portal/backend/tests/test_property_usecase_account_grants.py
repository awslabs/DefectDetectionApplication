"""
Properties 1-3 of portal-authorization-and-iam-scoping (design I1 "Test
strategy"; task 1.3): bounded, removal-correct account grants; refusal
before any STS call; creation gated exactly by CREATE_USECASES.

The real usecases handler on a private moto use-case table with the
recording STS / S3 / EventBridge fakes and the admitting attach validator of
usecase_account_fakes (module-scoped: hypothesis bodies may not use
function-scoped fixtures; every example starts from empty fakes and an
empty table).

Validates: Requirements 1.1, 1.3, 2.1, 2.6, 3.1, 3.2, 3.4
"""
import json
import re
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

import usecase_account_fakes as fx
from usecase_account_fakes import (
    COMPONENT_BUCKET, DATA, MOTO, PORTAL, TRUSTED, UNTRUSTED, access_role, api_event, call,
    data_role, sagemaker_role)

READ_SID = "AllowGreengrassDeviceRead-{}"
LIST_SID = "AllowGreengrassDeviceList-{}"
EVENT_SID = "AllowUseCaseAccount-{}"
# Use-case accounts the sequences draw from, all trusted for Property 1.
USECASE_ACCOUNTS = (TRUSTED, DATA, MOTO)
ACCOUNT_POOL = (TRUSTED, DATA, UNTRUSTED, MOTO)
ROLES = ("PortalAdmin", "UseCaseAdmin", "DataScientist", "Operator", "Viewer")
# The statement the shared-components provisioning leaves on the component
# bucket of any portal that provisioned once (its service statement naming
# the portal account); the account grants come and go beside it.
SERVICE_STATEMENT = {
    "Sid": "AllowGreengrassServiceAccess", "Effect": "Allow",
    "Principal": {"Service": "greengrass.amazonaws.com"},
    "Action": ["s3:GetObject", "s3:GetBucketLocation"],
    "Resource": [f"arn:aws:s3:::{COMPONENT_BUCKET}", f"arn:aws:s3:::{COMPONENT_BUCKET}/*"],
    "Condition": {"StringEquals": {"aws:SourceAccount": [PORTAL]}},
}
PROPERTY_SETTINGS = settings(max_examples=100, deadline=None,
                             suppress_health_check=[HealthCheck.too_slow])


@pytest.fixture(scope="module")
def h(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "account_trusted"), "this suite needs the real shared layer"
    sys.modules.pop("usecases", None)
    import usecases
    mp = pytest.MonkeyPatch()
    fakes = fx.install(usecases, shared_utils, mp)
    admin = fx.portal_admin(aws_stack)
    yield SimpleNamespace(uc=usecases, shared=shared_utils, stack=aws_stack, fakes=fakes, mp=mp,
                          admin=admin)
    mp.undo()


def fresh(h, trusted=TRUSTED, enforced=False):
    h.fakes.reset()
    fx.clear_usecases_table()
    h.mp.setenv("TRUSTED_USECASE_ACCOUNT_IDS", trusted)
    if enforced:
        h.mp.setenv(h.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
    else:
        h.mp.delenv(h.shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)


def handler(h, method, path, caller, usecase_id=None, body=None):
    status, payload, _ = call(h.uc, api_event(method, path, caller, usecase_id, body))
    return status, payload


def move_body(account):
    return {"account_id": account, "cross_account_role_arn": access_role(account),
            "external_id": f"ext-{account[:3]}", "sagemaker_execution_role_arn": sagemaker_role(account)}


# --------------------------------------------------------------- Property 1

class TestProperty1AccountGrantsAreBoundedAndRemovalCorrect:
    """**Feature: portal-authorization-and-iam-scoping, Property 1: Account
    grants are idempotent, bounded and removal-correct** — for any sequence
    of single- and multi-account creates, deletes and PUT account moves over
    trusted accounts, the component bucket policy holds exactly the two
    device statements of every currently referenced non-portal account, plus
    the portal account's pair once a single-account use case was created
    (never removed), beside the provisioning's service statement; and the
    EventBridge statement ids are one per referenced non-portal account plus
    the portal account's once created.
    Validates: Requirements 3.2, 3.4"""

    @PROPERTY_SETTINGS
    @given(data=st.data())
    def test_grants_follow_the_referenced_accounts(self, h, data):
        fresh(h, trusted=",".join(USECASE_ACCOUNTS))
        h.fakes.s3.seed([json.loads(json.dumps(SERVICE_STATEMENT))])
        live = {}                       # usecase_id -> account (PORTAL for single-account)
        portal_pair = False
        for _ in range(data.draw(st.integers(min_value=1, max_value=8), label="steps")):
            ops = ["create_single", "create_multi"] + (["delete", "move"] if live else [])
            op = data.draw(st.sampled_from(ops), label="op")
            event(op)
            if op == "create_single":
                status, payload = handler(h, "POST", "/usecases", h.admin, body=fx.single_account_body())
                assert status == 201, payload
                live[payload["usecase"]["usecase_id"]] = PORTAL
                portal_pair = True
            elif op == "create_multi":
                account = data.draw(st.sampled_from(USECASE_ACCOUNTS), label="account")
                status, payload = handler(h, "POST", "/usecases", h.admin,
                                          body=fx.multi_account_body(account))
                assert status == 201, payload
                live[payload["usecase"]["usecase_id"]] = account
            elif op == "delete":
                # Drawn by position in creation order: the ids are fresh
                # UUIDs on every run, so the choice must not depend on them.
                usecase_id = list(live)[data.draw(st.integers(0, len(live) - 1), label="delete")]
                status, payload = handler(h, "DELETE", f"/usecases/{usecase_id}", h.admin, usecase_id)
                assert status == 200, payload
                del live[usecase_id]
            else:
                usecase_id = list(live)[data.draw(st.integers(0, len(live) - 1), label="move")]
                new_account = data.draw(st.sampled_from(
                    [a for a in USECASE_ACCOUNTS if a != live[usecase_id]]), label="to")
                status, payload = handler(h, "PUT", f"/usecases/{usecase_id}", h.admin, usecase_id,
                                          move_body(new_account))
                assert status == 200, payload
                live[usecase_id] = new_account

            referenced = {account for account in live.values() if account != PORTAL}
            granted = referenced | ({PORTAL} if portal_pair else set())
            expected = sorted([READ_SID.format(a) for a in granted]
                              + [LIST_SID.format(a) for a in granted]
                              + [SERVICE_STATEMENT["Sid"]])
            assert h.fakes.s3.sids() == expected
            assert sorted(h.fakes.events.statements) == sorted(EVENT_SID.format(a) for a in granted)


# --------------------------------------------------------------- Property 2

def malformed_account_ids():
    return st.one_of(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=20),
        st.from_regex(r"\A[0-9]{11}\Z|\A[0-9]{13}\Z"),
        st.integers(min_value=1),
    ).filter(lambda value: not (isinstance(value, str) and re.fullmatch(r"\d{12}", value, re.ASCII)))


PINNED_ACCESS_ROLE = re.compile(r"arn:aws:iam::[0-9]{12}:role/DDAPortalAccessRole")


def malformed_role_arns(account, *, any_account):
    """Role ARNs that are not the pinned DDAPortalAccessRole of `account`;
    with `any_account` (verify-role derives the account from the ARN) not
    the pinned role of any account."""
    branches = [
        st.sampled_from([f"arn:aws:iam::{account}:role/SomeOtherRole",
                         f"arn:aws:iam::{account}:role/path/DDAPortalAccessRole",
                         f"arn:aws:iam::{account}:user/DDAPortalAccessRole",
                         "DDAPortalAccessRole"]),
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40),
    ]
    if not any_account:
        branches.append(st.sampled_from([a for a in ACCOUNT_POOL if a != account]).map(access_role))
    strategy = st.one_of(*branches).filter(lambda value: value != access_role(account))
    if any_account:
        strategy = strategy.filter(lambda value: not PINNED_ACCESS_ROLE.fullmatch(value))
    return strategy


class TestProperty2RefusalBeforeAnySTSCall:
    """**Feature: portal-authorization-and-iam-scoping, Property 2: Malformed
    or untrusted accounts are refused before any STS call** — for any
    non-conforming account id or role ARN, or any account outside a
    generated trusted list, POST /usecases, POST /usecases/verify-role and a
    PUT account move are refused 400 with INVALID_ACCOUNT_ID /
    INVALID_ROLE_ARN / ACCOUNT_NOT_TRUSTED, and no STS call and no write is
    made.
    Validates: Requirements 2.1, 2.6, 3.1"""

    @PROPERTY_SETTINGS
    @given(data=st.data())
    def test_refused_before_sts(self, h, data):
        trusted = data.draw(st.lists(st.sampled_from(ACCOUNT_POOL), min_size=1, unique=True),
                            label="trusted")
        fresh(h, trusted=",".join(trusted))
        account = data.draw(st.sampled_from(ACCOUNT_POOL), label="account")
        route = data.draw(st.sampled_from(["create", "verify", "move"]), label="route")
        kinds = ["role_arn"] + (["account_id", "sagemaker", "data_account"] if route != "verify" else [])
        untrusted = [a for a in ACCOUNT_POOL if a not in trusted]
        if untrusted:
            kinds.append("untrusted")
        kind = data.draw(st.sampled_from(kinds), label="kind")
        event(f"{route}:{kind}")

        fields = move_body(account)
        if kind == "untrusted":
            account = data.draw(st.sampled_from(untrusted), label="untrusted")
            fields = move_body(account)
        elif kind == "account_id":
            fields["account_id"] = data.draw(malformed_account_ids(), label="bad id")
        elif kind == "role_arn":
            fields["cross_account_role_arn"] = data.draw(
                malformed_role_arns(account, any_account=route == "verify"), label="bad arn")
        elif kind == "sagemaker":
            fields["sagemaker_execution_role_arn"] = data.draw(st.one_of(
                st.sampled_from([a for a in ACCOUNT_POOL if a != account]).map(sagemaker_role),
                st.sampled_from(["DDASageMakerExecutionRole", f"arn:aws:iam::{account}:user/x"])),
                label="bad sagemaker")
        else:
            data_account = data.draw(st.sampled_from([a for a in ACCOUNT_POOL if a != account]))
            fields.update(data.draw(st.sampled_from([
                {"data_account_id": data_account},
                {"data_account_id": data_account, "data_account_role_arn": access_role(data_account)},
                {"data_account_id": "data-account", "data_account_role_arn": data_role(data_account)},
            ]), label="bad data triple"))

        if route == "create":
            body = {**fx.multi_account_body(), **fields}
            status, payload = handler(h, "POST", "/usecases", h.admin, body=body)
        elif route == "verify":
            status, payload = handler(h, "POST", "/usecases/verify-role", h.admin, body={
                "role_arn": fields["cross_account_role_arn"], "external_id": "ext"})
        else:
            usecase_id = fx.put_multi_account_record(TRUSTED)
            status, payload = handler(h, "PUT", f"/usecases/{usecase_id}", h.admin, usecase_id, fields)

        assert status == 400, payload
        assert payload["error_code"] in ("INVALID_ACCOUNT_ID", "INVALID_ROLE_ARN", "ACCOUNT_NOT_TRUSTED")
        if kind == "untrusted":
            assert payload["error_code"] == "ACCOUNT_NOT_TRUSTED"
        assert h.fakes.sts.calls == []
        assert h.fakes.writes() == []


# --------------------------------------------------------------- Property 3

def holds_create_usecases(enforced, claim, row_role, row_status):
    """CREATE_USECASES is a PortalAdmin permission (D1). With enforcement off
    the legacy order applies (a PortalAdmin claim, or a global row naming
    PortalAdmin); with enforcement on only an enabled global row naming
    PortalAdmin grants it."""
    if enforced:
        return row_role == "PortalAdmin" and row_status in (None, "enabled")
    return claim == "PortalAdmin" or row_role == "PortalAdmin"


class TestProperty3CreationIsGatedByTheCreationPermission:
    """**Feature: portal-authorization-and-iam-scoping, Property 3: Creation
    and role verification are accepted iff the caller holds
    CREATE_USECASES** — for any generated registry state (claim, global
    row role and status) in both enforcement states, POST /usecases and POST
    /usecases/verify-role are accepted exactly when
    rbac_manager.has_permission(CREATE_USECASES) is true; a refusal is 403
    INSUFFICIENT_PERMISSIONS with no STS call and no write.
    Validates: Requirements 1.1, 1.3, 2.6"""

    @PROPERTY_SETTINGS
    @given(enforced=st.booleans(),
           claim=st.sampled_from(ROLES + ("unknown-role",)),
           row_role=st.one_of(st.none(), st.sampled_from(ROLES + ("unknown-role",))),
           row_status=st.sampled_from([None, "enabled", "disabled"]))
    def test_accepted_iff_has_permission(self, h, enforced, claim, row_role, row_status):
        fresh(h, enforced=enforced)
        caller = fx.user(h.stack, claim, "global" if row_role else None, row_role, row_status)
        holds = h.shared.rbac_manager.has_permission(
            caller["user_id"], "global", h.shared.Permission.CREATE_USECASES, caller)
        assert holds == holds_create_usecases(enforced, claim, row_role, row_status)
        event(f"enforced={enforced} holds={holds}")

        body = fx.multi_account_body(name=f"p3-{uuid.uuid4().hex[:8]}")
        status, payload = handler(h, "POST", "/usecases", caller, body=body)
        assert (status == 201) is holds, payload
        if not holds:
            assert status == 403 and payload["error_code"] == "INSUFFICIENT_PERMISSIONS"
            assert h.fakes.writes() == [] and h.fakes.sts.calls == []

        h.fakes.reset()
        status, payload = handler(h, "POST", "/usecases/verify-role", caller,
                                  body={"role_arn": access_role(TRUSTED), "external_id": "ext"})
        assert (status == 200) is holds, payload
        if not holds:
            assert status == 403 and payload["error_code"] == "INSUFFICIENT_PERMISSIONS"
            assert payload["status"] == "failed" and h.fakes.sts.calls == []
