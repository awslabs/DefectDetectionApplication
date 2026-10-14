"""
Labeling_Team management handlers in dda_labeling.py
(dda-data-labeling, task 4.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims, moto Cognito user pool for member identity/role/email
resolution):

- POST /labeling-teams: persistence scoped to the Use_Case (Req 3.1);
  empty / over-128-char / duplicate-per-use-case names rejected with the
  offending name and nothing persisted (Req 3.2)
- POST /labeling-teams/{teamId}/members: Data_Labeler role validation
  from the Cognito custom:role attribute and from per-usecase UserRoles
  rows (Req 3.3, 3.4), duplicate membership rejection leaving membership
  unchanged (Req 3.5), member persisted with identity and email
- DELETE /labeling-teams/{teamId}/members/{userId}: removal persisted
  (Req 3.6; reassignment wiring is task 7.2)
- DELETE /labeling-teams/{teamId}: rejected while an InProgress job
  references the team; allowed otherwise
- GET /labeling-teams?usecase_id=: only the Use_Case's teams, each with
  member identities and emails (Req 3.8)
- Non-admin callers are denied with an authorization error and no team
  data changed (Req 3.7, via the real @rbac_check path)
Cognito is a recording fake wired into the module under test (the
test_user_admin_* convention — moto's cognito-idp backend is not
available here); DynamoDB and the RBAC/audit paths are real moto.
"""
import json
import re
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
POOL_ID = "us-east-1_dda-test-pool"


# ----------------------------------------------------- fake Cognito client

class FakeCognitoClient:
    """Fake for the cognito-idp APIs dda_labeling uses: list_users with a
    `sub = "..."` filter and admin_get_user by username."""

    def __init__(self):
        self.users = {}  # username -> {attr name: value}

    def add_user(self, username, email, role=None):
        sub = str(uuid.uuid4())
        attrs = {"sub": sub, "email": email}
        if role:
            attrs["custom:role"] = role
        self.users[username] = attrs
        return sub

    @staticmethod
    def _shape(username, attrs, key):
        return {
            "Username": username,
            key: [{"Name": name, "Value": value}
                  for name, value in attrs.items()],
        }

    def list_users(self, UserPoolId=None, Filter=None, Limit=None):
        match = re.match(r'sub = "(.+)"', Filter or "")
        users = []
        if match:
            for username, attrs in self.users.items():
                if attrs["sub"] == match.group(1):
                    users.append(self._shape(username, attrs, "Attributes"))
        return {"Users": users[:Limit] if Limit else users}

    def admin_get_user(self, UserPoolId=None, Username=None):
        if Username not in self.users:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException",
                           "Message": "User does not exist."}},
                "AdminGetUser")
        return self._shape(Username, self.users[Username], "UserAttributes")


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with a
    fake Cognito client behind USER_POOL_ID."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID
    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito)


@pytest.fixture
def env(aws_stack, dda):
    """Per-test helper facade with a fresh Use_Case id."""
    return TeamsEnv(aws_stack, dda)


class TeamsEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.admin = self.jwt_user(role="UseCaseAdmin")

    # ------------------------------------------------------------ users
    def jwt_user(self, role="UseCaseAdmin"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def cognito_user(self, role="DataLabeler"):
        """A Cognito account (the member candidates the add-member modal
        lists); returns username, sub, and email."""
        username = f"labeler-{uuid.uuid4()}"
        email = f"{username}@example.com"
        sub = self.dda.cognito.add_user(username, email, role=role)
        return SimpleNamespace(username=username, sub=sub,
                               email=email, role=role)

    # ----------------------------------------------------------- events
    def event(self, method, resource, user, path_params=None,
              query=None, body=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def invoke(self, method, resource, user=None, path_params=None,
               query=None, body=None):
        response = self.dda.module.handler(
            self.event(method, resource, user or self.admin,
                       path_params, query, body), None)
        return response["statusCode"], json.loads(response["body"])

    # ---------------------------------------------------------- actions
    def create_team(self, name, usecase_id=None, user=None):
        return self.invoke(
            "POST", "/labeling-teams", user=user,
            body={"usecase_id": usecase_id or self.usecase_id,
                  "team_name": name})

    def list_teams(self, usecase_id=None, user=None):
        return self.invoke(
            "GET", "/labeling-teams", user=user,
            query={"usecase_id": usecase_id or self.usecase_id})

    def delete_team(self, team_id, user=None):
        return self.invoke(
            "DELETE", "/labeling-teams/{teamId}", user=user,
            path_params={"teamId": team_id})

    def add_member(self, team_id, user_id, user=None):
        return self.invoke(
            "POST", "/labeling-teams/{teamId}/members", user=user,
            path_params={"teamId": team_id}, body={"user_id": user_id})

    def remove_member(self, team_id, user_id, user=None):
        return self.invoke(
            "DELETE", "/labeling-teams/{teamId}/members/{userId}",
            user=user, path_params={"teamId": team_id, "userId": user_id})

    # ------------------------------------------------------------ store
    def team_items(self, team_id):
        response = self.stack.tables.labeling_teams.query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("team_id").eq(team_id)))
        return response.get("Items", [])

    def usecase_metas(self, usecase_id=None):
        response = self.stack.tables.labeling_teams.query(
            IndexName="usecase-teams-index",
            KeyConditionExpression=boto3.dynamodb.conditions.Key(
                "usecase_id").eq(usecase_id or self.usecase_id))
        return response.get("Items", [])

    def put_job(self, team_id, status="InProgress", usecase_id=None):
        job_id = f"job-{uuid.uuid4()}"
        self.stack.tables.labeling_jobs.put_item(Item={
            "job_id": job_id,
            "usecase_id": usecase_id or self.usecase_id,
            "created_at": 1,
            "status": status,
            "team_id": team_id,
            "labeling_backend": "DDA",
        })
        return job_id


# ----------------------------------------------------------- team creation

class TestCreateTeam:
    def test_create_persists_team_scoped_to_usecase(self, env):
        """Req 3.1: the team is persisted scoped to its Use_Case."""
        status, body = env.create_team("Inspection Team A")
        assert status == 201
        team = body["team"]
        assert team["usecase_id"] == env.usecase_id
        assert team["team_name"] == "Inspection Team A"
        assert team["members"] == []

        metas = env.usecase_metas()
        assert [m["team_name"] for m in metas] == ["Inspection Team A"]
        assert metas[0]["created_by"] == env.admin["user_id"]

    def test_empty_name_rejected_nothing_persisted(self, env):
        """Req 3.2: empty name -> validation error, no team created."""
        for name in ("", "   "):
            status, body = env.create_team(name)
            assert status == 400
            assert "empty" in body["error"].lower()
        assert env.usecase_metas() == []

    def test_name_over_128_chars_rejected(self, env):
        """Req 3.2: names longer than 128 characters are rejected with the
        offending name; a 128-character name is accepted."""
        status, body = env.create_team("x" * 129)
        assert status == 400
        assert body["team_name"] == "x" * 129
        assert env.usecase_metas() == []

        status, _ = env.create_team("x" * 128)
        assert status == 201

    def test_duplicate_name_in_same_usecase_rejected(self, env):
        """Req 3.2: per-use-case name uniqueness; the same name is fine in
        a different Use_Case."""
        status, _ = env.create_team("Night Shift")
        assert status == 201

        status, body = env.create_team("Night Shift")
        assert status == 400
        assert "Night Shift" in body["error"]
        assert len(env.usecase_metas()) == 1

        other_usecase = f"uc-{uuid.uuid4()}"
        status, _ = env.create_team("Night Shift", usecase_id=other_usecase)
        assert status == 201

    def test_non_admin_denied_with_authorization_error(self, env):
        """Req 3.7: a non-admin caller is rejected and no team data
        changes (real @rbac_check path)."""
        scientist = env.jwt_user(role="DataScientist")
        status, body = env.create_team("Blocked Team", user=scientist)
        assert status == 403
        assert body["error"] == "Insufficient permissions"
        assert env.usecase_metas() == []


# --------------------------------------------------------------- listing

class TestListTeams:
    def test_lists_only_usecase_teams_with_member_identity_and_email(
            self, env):
        """Req 3.8: only the Use_Case's teams, with each member's user
        identity and email address."""
        _, created = env.create_team("Team One")
        team_id = created["team"]["team_id"]
        labeler = env.cognito_user()
        status, _ = env.add_member(team_id, labeler.sub)
        assert status == 201

        # A team in another Use_Case must not appear.
        env.create_team("Other Team", usecase_id=f"uc-{uuid.uuid4()}")

        status, body = env.list_teams()
        assert status == 200
        assert body["count"] == 1
        team = body["teams"][0]
        assert team["team_name"] == "Team One"
        assert team["members"] == [{
            "user_id": labeler.sub,
            "email": labeler.email,
            "added_at": team["members"][0]["added_at"],
        }]

    def test_empty_usecase_returns_empty_list(self, env):
        status, body = env.list_teams()
        assert status == 200
        assert body == {"teams": [], "count": 0}


# --------------------------------------------------------- member addition

class TestAddMember:
    def test_add_data_labeler_persists_member_with_email(self, env):
        """Req 3.3: a Data_Labeler is persisted as a member with their
        portal account email."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        labeler = env.cognito_user(role="DataLabeler")

        status, body = env.add_member(team_id, labeler.sub)
        assert status == 201
        assert body["member"]["user_id"] == labeler.sub
        assert body["member"]["email"] == labeler.email

        items = env.team_items(team_id)
        member_items = [i for i in items if i["sk"].startswith("MEMBER#")]
        assert len(member_items) == 1
        assert member_items[0]["user_id"] == labeler.sub
        assert member_items[0]["email"] == labeler.email

    def test_non_data_labeler_rejected_membership_unchanged(self, env):
        """Req 3.4: adding a user without the Data_Labeler role fails with
        a validation error naming the missing role."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        viewer = env.cognito_user(role="Viewer")

        status, body = env.add_member(team_id, viewer.sub)
        assert status == 400
        assert body["required_role"] == "DataLabeler"
        assert [i for i in env.team_items(team_id)
                if i["sk"].startswith("MEMBER#")] == []

    def test_usecase_scoped_user_roles_row_grants_data_labeler(self, env):
        """Req 3.3/2.1: a per-usecase UserRoles row (user_roles.py
        assignment path) also satisfies the Data_Labeler check."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        account = env.cognito_user(role=None)  # no custom:role attribute
        env.stack.tables.user_roles.put_item(Item={
            "user_id": account.sub,
            "usecase_id": env.usecase_id,
            "role": "DataLabeler",
        })

        status, _ = env.add_member(team_id, account.sub)
        assert status == 201

    def test_duplicate_membership_rejected_unchanged(self, env):
        """Req 3.5: adding an existing member fails and leaves the
        membership unchanged."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        labeler = env.cognito_user()
        assert env.add_member(team_id, labeler.sub)[0] == 201

        status, body = env.add_member(team_id, labeler.sub)
        assert status == 409
        assert "already a member" in body["error"]
        assert len([i for i in env.team_items(team_id)
                    if i["sk"].startswith("MEMBER#")]) == 1

    def test_unknown_user_404(self, env):
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        status, body = env.add_member(team_id, f"nobody-{uuid.uuid4()}")
        assert status == 404
        assert body["error"] == "User not found"

    def test_unknown_team_404(self, env):
        labeler = env.cognito_user()
        status, _ = env.add_member(f"team-{uuid.uuid4()}", labeler.sub)
        assert status == 404


# ---------------------------------------------------------- member removal

class TestRemoveMember:
    def test_removal_persisted(self, env):
        """Req 3.6: the removal is persisted (reassignment is task 7.2)."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        labeler = env.cognito_user()
        assert env.add_member(team_id, labeler.sub)[0] == 201

        status, _ = env.remove_member(team_id, labeler.sub)
        assert status == 200
        assert [i for i in env.team_items(team_id)
                if i["sk"].startswith("MEMBER#")] == []

    def test_removing_non_member_404(self, env):
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        status, body = env.remove_member(team_id, f"user-{uuid.uuid4()}")
        assert status == 404
        assert "not a member" in body["error"]


# ------------------------------------------------------------ team deletion

class TestDeleteTeam:
    def test_delete_rejected_while_in_progress_job_references_team(
            self, env):
        """A team referenced by an InProgress job cannot be deleted."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        job_id = env.put_job(team_id, status="InProgress")

        status, body = env.delete_team(team_id)
        assert status == 409
        assert job_id in body["in_progress_job_ids"]
        assert len(env.usecase_metas()) == 1  # team unchanged

    def test_delete_succeeds_without_in_progress_reference(self, env):
        """Completed jobs and other teams' InProgress jobs don't block."""
        _, created = env.create_team("Team")
        team_id = created["team"]["team_id"]
        labeler = env.cognito_user()
        assert env.add_member(team_id, labeler.sub)[0] == 201
        env.put_job(team_id, status="Completed")
        env.put_job(f"team-{uuid.uuid4()}", status="InProgress")

        status, _ = env.delete_team(team_id)
        assert status == 200
        assert env.team_items(team_id) == []  # META and members removed

    def test_delete_unknown_team_404(self, env):
        status, _ = env.delete_team(f"team-{uuid.uuid4()}")
        assert status == 404
