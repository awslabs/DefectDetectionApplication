"""
Unit tests for the Setup_Token service (station-quick-setup task 2.1).

These are example-based checks that exercise generate_token / parse_token /
validate_token in isolation using an injected in-memory registration loader,
so no AWS is required. The numbered Hypothesis property tests (Properties
6-8) live in their own reserved test tasks.
"""
import token_service as ts
from token_service import ValidationResult


def _registration(secret, *, now=1_000_000, ttl=ts.TOKEN_TTL_SECONDS,
                  consumed_at=0, token_hash=None):
    return {
        "registration_id": "reg-1",
        "token_hash": token_hash if token_hash is not None
        else ts._sha256_hex(secret),
        "token_expires_at": now + ttl,
        "consumed_at": consumed_at,
    }


def _loader_for(item):
    return lambda registration_id: item


def test_generate_token_shape_hash_and_ttl():
    now = 1_000_000
    token, token_hash, expires_at = ts.generate_token("reg-1", now=now)

    prefix, reg_id, secret = token.split(".")
    assert prefix == ts.TOKEN_PREFIX
    assert reg_id == "reg-1"
    # 32 bytes url-safe base64 -> 43 chars, comfortably >= 128 bits entropy.
    assert len(secret) >= 40
    # Stored hash is the hash of the SECRET, not the whole token (Req 3.6).
    assert token_hash == ts._sha256_hex(secret)
    assert token_hash != ts._sha256_hex(token)
    # TTL bounded at exactly 90 minutes (Req 3.1).
    assert expires_at == now + 90 * 60


def test_generate_token_secrets_are_unique():
    t1, h1, _ = ts.generate_token("reg-1")
    t2, h2, _ = ts.generate_token("reg-1")
    assert t1 != t2
    assert h1 != h2


def test_valid_token_validates():
    token, token_hash, expires_at = ts.generate_token("reg-1", now=1_000_000)
    item = {"token_hash": token_hash, "token_expires_at": expires_at,
            "consumed_at": 0}
    out = ts.validate_token(token, now=1_000_100, load_registration=_loader_for(item))
    assert out.result is ValidationResult.VALID
    assert out.registration is item


def test_expired_token_is_expired_and_returns_registration():
    token, token_hash, expires_at = ts.generate_token("reg-1", now=1_000_000)
    item = {"token_hash": token_hash, "token_expires_at": expires_at,
            "consumed_at": 0}
    # Present exactly at expiry and after — both expired.
    out_at = ts.validate_token(token, now=expires_at, load_registration=_loader_for(item))
    out_after = ts.validate_token(token, now=expires_at + 1, load_registration=_loader_for(item))
    assert out_at.result is ValidationResult.EXPIRED
    assert out_after.result is ValidationResult.EXPIRED
    assert out_after.registration is item


def test_consumed_token_is_invalid_even_before_expiry():
    token, token_hash, expires_at = ts.generate_token("reg-1", now=1_000_000)
    item = {"token_hash": token_hash, "token_expires_at": expires_at,
            "consumed_at": 1_000_050}
    out = ts.validate_token(token, now=1_000_100, load_registration=_loader_for(item))
    assert out.result is ValidationResult.INVALID
    assert out.registration is None


def test_consumed_and_expired_collapses_to_invalid():
    # Priority: consumed must win over expired to stay indistinguishable.
    token, token_hash, expires_at = ts.generate_token("reg-1", now=1_000_000)
    item = {"token_hash": token_hash, "token_expires_at": expires_at,
            "consumed_at": 1_000_050}
    out = ts.validate_token(token, now=expires_at + 5, load_registration=_loader_for(item))
    assert out.result is ValidationResult.INVALID


def test_unknown_registration_is_invalid():
    token, _, _ = ts.generate_token("reg-1", now=1_000_000)
    out = ts.validate_token(token, now=1_000_100, load_registration=lambda rid: None)
    assert out.result is ValidationResult.INVALID
    assert out.registration is None


def test_wrong_secret_is_invalid():
    _, token_hash, expires_at = ts.generate_token("reg-1", now=1_000_000)
    item = {"token_hash": token_hash, "token_expires_at": expires_at,
            "consumed_at": 0}
    # A different token (different secret) for the same registration id.
    other_token, _, _ = ts.generate_token("reg-1", now=1_000_000)
    out = ts.validate_token(other_token, now=1_000_100, load_registration=_loader_for(item))
    assert out.result is ValidationResult.INVALID


def test_superseded_token_is_invalid():
    # Old token no longer matches the registration's regenerated hash.
    old_token, _old_hash, _ = ts.generate_token("reg-1", now=1_000_000)
    _new_token, new_hash, new_expires = ts.generate_token("reg-1", now=1_000_200)
    item = {"token_hash": new_hash, "token_expires_at": new_expires,
            "consumed_at": 0}
    out = ts.validate_token(old_token, now=1_000_300, load_registration=_loader_for(item))
    assert out.result is ValidationResult.INVALID


def test_storage_error_returns_check_failed():
    token, _, _ = ts.generate_token("reg-1", now=1_000_000)

    def boom(_rid):
        raise RuntimeError("dynamo unavailable")

    out = ts.validate_token(token, now=1_000_100, load_registration=boom)
    assert out.result is ValidationResult.CHECK_FAILED
    assert out.registration is None


def test_malformed_tokens_are_invalid():
    item = {"token_hash": "x", "token_expires_at": 2_000_000, "consumed_at": 0}
    loader = _loader_for(item)
    for bad in ["", "not-a-token", "dqs1.reg-1", "dqs1..secret",
                "dqs1.reg-1.secret.extra", "wrongprefix.reg-1.secret", None]:
        out = ts.validate_token(bad, now=1_000_000, load_registration=loader)
        assert out.result is ValidationResult.INVALID, bad


def test_parse_token_roundtrip():
    token, _, _ = ts.generate_token("abc-123", now=1)
    reg_id, secret = ts.parse_token(token)
    assert reg_id == "abc-123"
    assert token == f"{ts.TOKEN_PREFIX}.{reg_id}.{secret}"
