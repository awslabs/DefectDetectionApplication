# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Property tests for the pure Ubuntu flavor domain logic in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/ubuntu-pro-build-servers (tasks 1.3, 1.4, 1.5)

**Validates: Requirements 6.1, 6.3, 6.4, 6.5, 6.6**

The expected behavior is restated here independently of the
implementation:

- *For any* configured default flavor d in {pro, standard} and any
  request value, ``resolve_effective_ubuntu_flavor`` returns the
  request's flavor when present, else d; omitting the flavor is
  indistinguishable from explicitly passing d (Property 10; Req 6.1,
  6.3, 6.4).
- *For any* ``ubuntu_flavor`` update value that is not exactly ``pro``
  or ``standard``, and any prior stored configuration,
  ``apply_config_update`` rejects the update with a validation error
  naming the supported values and returns the stored configuration
  unchanged (Property 11; Req 6.5).
- *For any* stored ``ubuntu_flavor`` value that is not exactly ``pro``
  or ``standard`` and a request omitting the flavor,
  ``resolve_effective_ubuntu_flavor`` returns no flavor and a
  ``config_default_flavor_invalid`` error identifying the invalid stored
  default, so the handler rejects before any EC2 call (Property 12;
  Req 6.6).

Pure ``build_domain`` tests, no mocks (design Testing Strategy: the
pure-function properties 10, 11, and 12's resolver clause run against
``build_domain`` directly).
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure domain module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402

# ---------------------------------------------------------------------------
# Independent restatement of the supported Ubuntu_Flavor values
# ---------------------------------------------------------------------------

_VALID_FLAVORS = ("pro", "standard")

_VALID_FLAVOR = st.sampled_from(_VALID_FLAVORS)

# Invalid flavor values: strings that are not exactly 'pro'/'standard'
# (empty, wrong case, whitespace-padded, arbitrary text) and non-string
# values. Exact case-sensitive matching is the requirement, so 'Pro' and
# 'STANDARD' are invalid.
_INVALID_FLAVOR = st.one_of(
    st.sampled_from([
        "", "Pro", "PRO", "Standard", "STANDARD", " pro", "pro ",
        "ubuntu-pro", "std", "PRO ",
    ]),
    st.text(min_size=1, max_size=20).filter(
        lambda s: s not in _VALID_FLAVORS),
    st.integers(),
    st.floats(allow_nan=False),
    st.booleans(),
)

# Prior stored configurations for the atomic-reject property: a random
# subset of parameters with simple stored values (or nothing stored).
_STORED_VALUES = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=999),
    st.sampled_from(["m6g.4xlarge", "eu-central-1", "pro", "standard",
                     "custom-value"]),
)


@st.composite
def _stored_configs(draw):
    if draw(st.booleans()):
        fields = draw(st.lists(
            st.sampled_from(sorted(build_domain.DEFAULT_BUILD_CONFIG)),
            unique=True,
            max_size=len(build_domain.DEFAULT_BUILD_CONFIG),
        ))
        return {field: draw(_STORED_VALUES) for field in fields}
    return None  # configuration never written


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------

# Feature: ubuntu-pro-build-servers, Property 10: The configured default
# applies exactly as an explicit selection
# Validates: Requirements 6.1, 6.3, 6.4
@settings(max_examples=100)
@given(
    configured_default=_VALID_FLAVOR,
    requested=st.one_of(st.none(), _VALID_FLAVOR),
    other_default=_VALID_FLAVOR,
)
def test_configured_default_applies_exactly_as_explicit_selection(
        configured_default, requested, other_default):
    """For any configured default d in {pro, standard} and any request
    value, the effective flavor equals the request's ubuntu_flavor when
    present, else d (Req 6.1, 6.3, 6.4); a request omitting the flavor
    is indistinguishable from one explicitly carrying d."""
    flavor, errors = build_domain.resolve_effective_ubuntu_flavor(
        requested, configured_default)

    expected = requested if requested is not None else configured_default
    assert flavor == expected, (
        "resolve_effective_ubuntu_flavor(%r, %r) resolved %r, expected %r"
        % (requested, configured_default, flavor, expected)
    )
    assert errors == [], (
        "a valid resolution must carry no errors: %r" % (errors,)
    )

    # Omitting the flavor is indistinguishable from explicitly passing
    # the configured default d — including when the stored default were
    # any other value (an explicit request always wins, Req 6.3).
    omitted = build_domain.resolve_effective_ubuntu_flavor(
        None, configured_default)
    explicit = build_domain.resolve_effective_ubuntu_flavor(
        configured_default, other_default)
    assert omitted == explicit == (configured_default, []), (
        "omitting the flavor (default %r) resolved %r, explicitly "
        "passing it resolved %r" % (configured_default, omitted, explicit)
    )


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------

# Feature: ubuntu-pro-build-servers, Property 11: Config updates with an
# invalid flavor are rejected atomically
# Validates: Requirements 6.5
@settings(max_examples=100)
@given(
    invalid_value=_INVALID_FLAVOR,
    stored=_stored_configs(),
    valid_rider=st.one_of(st.none(),
                          st.integers(min_value=201, max_value=999)),
)
def test_invalid_config_flavor_rejected_atomically(
        invalid_value, stored, valid_rider):
    """For any non-pro/standard ubuntu_flavor update value and any prior
    stored configuration, apply_config_update rejects with an error
    naming the supported values and returns the stored configuration
    unchanged — even when the update also carries an individually valid
    field (atomic reject, Req 6.5)."""
    update = {"ubuntu_flavor": invalid_value}
    if valid_rider is not None:
        update["volume_size_gb"] = valid_rider

    before = dict(stored) if stored else {}
    new_stored, result = build_domain.apply_config_update(stored, update)

    assert not result.valid, (
        "apply_config_update accepted invalid ubuntu_flavor %r"
        % (invalid_value,)
    )
    flavor_errors = [e for e in result.errors
                     if e.get("parameter") == "ubuntu_flavor"]
    assert len(flavor_errors) == 1, result.errors
    error = flavor_errors[0]
    assert error["rule"] == build_domain.RULE_CONFIG_UBUNTU_FLAVOR_INVALID
    # The error names both supported values (Req 6.5).
    assert "'pro'" in error["message"], error
    assert "'standard'" in error["message"], error

    # Atomic reject: the stored configuration is unchanged; no field of
    # the rejected update is applied, even the individually valid rider.
    assert new_stored == before, (
        "rejected update mutated the stored configuration: %r -> %r "
        "(update=%r)" % (before, new_stored, update)
    )
    # The original input object is also untouched.
    assert (dict(stored) if stored else {}) == before


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

# Feature: ubuntu-pro-build-servers, Property 12: An invalid stored
# default fails launches closed
# Validates: Requirements 6.6
@settings(max_examples=100)
@given(invalid_default=_INVALID_FLAVOR)
def test_invalid_stored_default_fails_closed(invalid_default):
    """For any stored ubuntu_flavor value not exactly pro or standard
    and a request omitting the flavor, resolve_effective_ubuntu_flavor
    returns no flavor and a config_default_flavor_invalid error
    identifying the invalid stored default (Req 6.6) — the handler
    therefore rejects before any EC2 call."""
    flavor, errors = build_domain.resolve_effective_ubuntu_flavor(
        None, invalid_default)

    assert flavor is None, (
        "an invalid stored default %r must not resolve to a flavor, "
        "got %r" % (invalid_default, flavor)
    )
    assert len(errors) == 1, errors
    error = errors[0]
    assert error["rule"] == build_domain.RULE_CONFIG_DEFAULT_FLAVOR_INVALID
    # The error identifies the invalid stored default value (Req 6.6).
    assert str(invalid_default) in error["message"], error
