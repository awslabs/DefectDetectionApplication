"""
get_user_from_event: the `email` handlers persist as `created_by` must never
be the literal 'unknown' when the token carries a real identifier.

Background: the bootstrap `admin` Cognito user has no `email` attribute, so
its ID token has no email claim and the JWT authorizer forwards 'unknown';
every training/import/vLLM record that user created showed
"Created By: unknown". Fallback order: email → username → sub.
"""
import shared_utils as su


def _claims_event(**claims):
    return {"requestContext": {"authorizer": {"claims": claims}}}


def _context_event(**ctx):
    return {"requestContext": {"authorizer": ctx}}


def test_cognito_claims_with_email_unchanged():
    user = su.get_user_from_event(_claims_event(
        sub="sub-1", email="ryvan@amazon.com", **{"cognito:username": "ryvan"}))
    assert user == {"user_id": "sub-1", "email": "ryvan@amazon.com",
                    "username": "ryvan", "role": "Viewer"}


def test_cognito_claims_without_email_falls_back_to_username():
    user = su.get_user_from_event(_claims_event(
        sub="a4b804e8", **{"cognito:username": "admin", "custom:role": "PortalAdmin"}))
    assert user["email"] == "admin"
    assert user["username"] == "admin"
    assert user["role"] == "PortalAdmin"


def test_cognito_claims_without_email_or_username_falls_back_to_sub():
    user = su.get_user_from_event(_claims_event(sub="a4b804e8"))
    assert user["email"] == "a4b804e8"


def test_jwt_authorizer_context_forwarding_literal_unknown_is_replaced():
    """The authorizer writes the STRING 'unknown' for a missing email; the
    shared layer must not persist that when a username exists."""
    user = su.get_user_from_event(_context_event(
        userId="a4b804e8", email="unknown", username="admin", role="PortalAdmin"))
    assert user["email"] == "admin"
    assert user["user_id"] == "a4b804e8"


def test_jwt_authorizer_context_with_email_unchanged():
    user = su.get_user_from_event(_context_event(
        userId="s", email="ops@local", username="ops", role="Operator"))
    assert user["email"] == "ops@local"


def test_blank_email_is_treated_as_missing():
    user = su.get_user_from_event(_claims_event(sub="s", email="   ", **{"cognito:username": "admin"}))
    assert user["email"] == "admin"


def test_no_authorizer_still_returns_unknown_everywhere():
    user = su.get_user_from_event({"requestContext": {}})
    assert user == {"user_id": "unknown", "email": "unknown",
                    "username": "unknown", "role": "Viewer"}
