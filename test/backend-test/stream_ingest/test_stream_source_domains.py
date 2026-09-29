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
"""The device's stream camera rules equal the Portal's
(rtsp-rtmp-stream-cameras task 15.5 — Requirements 4.1, 4.2, 5.5).

The Portal (``camera_registry.py``, ``camera_sync.py``,
``stream_credentials.py``) and the device (``model/stream_source.py``)
each keep a literal copy of the stream setting domains, the per-type
settings, the defaults and the credential fields, because neither side
can import the other. A camera the Portal accepts must be one the device
accepts, and a converged camera must compare equal on both sides, so the
copies are pinned equal here. The Portal files are read with ``ast``:
importing them would pull in boto3 and the Lambda environment.

Also pinned: every rule the device applies is total over the value
domains (each boundary accepted, each value just outside rejected with a
message naming the field and never the value).
"""
import ast
import os

import pytest

from model import stream_source
from model.stream_source import StreamSourceError, normalize_stream_settings, validate_credentials

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_PORTAL_FUNCTIONS = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")


def _literal_constants(file_name):
    """Module-level ``NAME = <literal>`` (or annotated) assignments."""
    path = os.path.join(_PORTAL_FUNCTIONS, file_name)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            name, value = node.target.id, node.value
        else:
            continue
        try:
            constants[name] = ast.literal_eval(value)
        except ValueError:
            continue
    return constants


@pytest.fixture(scope="module")
def registry():
    return _literal_constants("camera_registry.py")


@pytest.fixture(scope="module")
def sync():
    return _literal_constants("camera_sync.py")


@pytest.fixture(scope="module")
def portal_credentials():
    return _literal_constants("stream_credentials.py")


class TestPortalParity:
    @pytest.mark.parametrize("portal_name, device_name", [
        ("STREAM_TRANSPORTS", "TRANSPORTS"),
        ("STREAM_DECODER_POLICIES", "DECODER_POLICIES"),
        ("STREAM_LATENCY_MS_RANGE", "LATENCY_MS_RANGE"),
        ("STREAM_MAX_FRAME_DIMENSION_RANGE", "MAX_FRAME_DIMENSION_RANGE"),
        ("STREAM_STALL_TIMEOUT_S_RANGE", "STALL_TIMEOUT_S_RANGE"),
    ])
    def test_value_domains_are_equal(self, registry, portal_name, device_name):
        assert registry[portal_name] == getattr(stream_source, device_name)

    @pytest.mark.parametrize("source_type", ["RTSP", "RTMP"])
    def test_each_type_has_the_same_settings_in_the_same_order(self, registry, source_type):
        portal_settings = tuple(name for name in registry["STREAM_PARAMS_BY_TYPE"][source_type]
                                if name != "url")
        assert tuple(normalize_stream_settings(source_type, {})) == portal_settings

    def test_the_stream_types_are_the_same(self, registry):
        assert tuple(registry["STREAM_PARAMS_BY_TYPE"]) == stream_source.STREAM_SOURCE_TYPE_VALUES

    @pytest.mark.parametrize("source_type", ["RTSP", "RTMP"])
    def test_the_defaults_the_portal_compares_against_are_the_device_defaults(self, sync, source_type):
        portal_defaults = {name: value for name, value in
                           sync["_STREAM_PARAM_DEFAULTS"][source_type].items()
                           if name != "credentialsConfigured"}
        assert normalize_stream_settings(source_type, {}) == portal_defaults

    def test_the_credential_fields_and_their_bound_are_the_same(self, portal_credentials):
        assert portal_credentials["CREDENTIAL_FIELDS"] == stream_source.CREDENTIAL_FIELDS
        assert portal_credentials["MAX_CREDENTIAL_FIELD_LENGTH"] == \
            stream_source.MAX_CREDENTIAL_FIELD_LENGTH

    def test_device_managed_settings_are_portal_managed_too(self, registry):
        assert set(stream_source.MANAGED_SETTINGS) <= set(registry["STREAM_SERVER_MANAGED_PARAMS"])

    def test_the_portal_treats_every_device_credential_field_as_credential_material(self, registry):
        assert set(stream_source.CREDENTIAL_FIELDS) <= set(registry["STREAM_CREDENTIAL_PARAMS"])


def _bounds():
    return [("latencyMs", stream_source.LATENCY_MS_RANGE, "RTSP"),
            ("maxFrameDimension", stream_source.MAX_FRAME_DIMENSION_RANGE, "RTMP"),
            ("stallTimeoutS", stream_source.STALL_TIMEOUT_S_RANGE, "RTSP")]


class TestDomainsAreEnforced:
    @pytest.mark.parametrize("name, bounds, source_type", _bounds())
    def test_integer_bounds_are_inclusive(self, name, bounds, source_type):
        low, high = bounds
        assert normalize_stream_settings(source_type, {name: low})[name] == low
        assert normalize_stream_settings(source_type, {name: high})[name] == high
        for outside in (low - 1, high + 1):
            with pytest.raises(StreamSourceError) as raised:
                normalize_stream_settings(source_type, {name: outside})
            assert raised.value.field == f"streamSettings.{name}"
            assert str(outside) not in raised.value.message

    @pytest.mark.parametrize("value", [True, 1.5, "200", [200]])
    def test_integer_settings_reject_other_types(self, value):
        with pytest.raises(StreamSourceError) as raised:
            normalize_stream_settings("RTSP", {"latencyMs": value})
        assert raised.value.field == "streamSettings.latencyMs"

    @pytest.mark.parametrize("name, values", [("transport", stream_source.TRANSPORTS),
                                              ("decoder", stream_source.DECODER_POLICIES)])
    def test_enumerations_accept_exactly_their_values(self, name, values):
        for value in values:
            assert normalize_stream_settings("RTSP", {name: value})[name] == value
        for value in ("TCP", "", "hw", 0):
            with pytest.raises(StreamSourceError) as raised:
                normalize_stream_settings("RTSP", {name: value})
            assert raised.value.field == f"streamSettings.{name}"

    def test_none_returns_a_setting_to_its_default(self):
        stored = normalize_stream_settings("RTSP", {"latencyMs": 900})
        assert normalize_stream_settings("RTSP", {"latencyMs": None}, base=stored)["latencyMs"] == 200

    def test_managed_settings_carry_over_but_cannot_be_set(self):
        base = dict(normalize_stream_settings("RTSP", {}), credentialRef="ref-1",
                    credentialsUpdatedAt=1700000000000)
        merged = normalize_stream_settings("RTSP", {"decoder": "software"}, base=base)
        assert merged["credentialRef"] == "ref-1" and merged["decoder"] == "software"
        for name in stream_source.MANAGED_SETTINGS:
            with pytest.raises(StreamSourceError) as raised:
                normalize_stream_settings("RTSP", {name: "x"})
            assert raised.value.field == f"streamSettings.{name}"

    def test_credential_values_never_appear_in_a_message(self):
        secret = "pw\x01-DOMAIN-3c7e"
        for credentials in ({"password": secret}, {"password": secret * 200},
                            {"token": secret}, {"password": 7}):
            with pytest.raises(StreamSourceError) as raised:
                validate_credentials(credentials)
            assert "DOMAIN-3c7e" not in str(raised.value)
            assert raised.value.field.startswith("credentials")
