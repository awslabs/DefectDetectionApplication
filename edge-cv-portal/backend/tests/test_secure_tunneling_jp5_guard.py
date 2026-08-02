"""Unit tests for the JP5 SecureTunneling version guard (devices.py).

JetPack 5 devices run Ubuntu 20.04 / GLIBC 2.31. The AWS-managed
``aws.greengrass.SecureTunneling`` >= 2.0.0 is built against GLIBC >= 2.32 and
crash-loops on JP5 ("GLIBC_2.32 not found"), which fails the whole
thing-targeted deployment and triggers a rollback. The portal therefore caps
SecureTunneling to ``SECURE_TUNNELING_MAX_JP5`` (1.1.3) when — and only when —
the target device is an ``arm64_jp5`` variant. Other arches (arm64_jp6,
x86_64) must be unaffected.

These exercise:
- ``_semver_tuple``      : version parsing / ordering.
- ``_cap_secure_tunneling_version`` : the arch-scoped cap (pure).
- ``_device_local_server_arch``     : arch detection from the installed
                                       LocalServer component name.
- ``set_ssh_tunnel``     : end-to-end, the version actually written into the
                           created Greengrass deployment is capped for JP5 and
                           left at latest for JP6.

The module is imported through the shared moto-backed session fixture so its
module-level boto3 resources bind to the mock (same pattern as the other
device tests).
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture(scope="module")
def devices(aws_stack):
    """Import devices inside the moto mock so its module-level boto3
    resources are intercepted."""
    for module_name in ("devices", "deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import devices

    return devices


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeGreengrass:
    """Minimal greengrassv2 stand-in for the SecureTunneling guard paths.

    - list_installed_components -> paginator yielding the configured
      LocalServer component (drives arch detection).
    - list_deployments / get_deployment -> the current thing components.
    - create_deployment -> records the components it was asked to deploy.
    """

    def __init__(self, local_server_name, current_components=None):
        self._local_server_name = local_server_name
        self._current_components = current_components or {}
        self.created = []

    # --- paginator plumbing --------------------------------------------
    def get_paginator(self, name):
        assert name == "list_installed_components"
        installed = []
        if self._local_server_name:
            installed.append({
                "componentName": self._local_server_name,
                "componentVersion": "1.0.34",
            })
        pages = [{"installedComponents": installed}]

        class _P:
            def paginate(self, **kwargs):
                return iter(pages)

        return _P()

    # --- current thing deployment --------------------------------------
    def list_deployments(self, **kwargs):
        return {"deployments": [{"deploymentId": "dep-existing"}]}

    def get_deployment(self, **kwargs):
        return {"components": dict(self._current_components)}

    def create_deployment(self, **kwargs):
        self.created.append(kwargs)
        return {"deploymentId": "dep-new", "iotJobId": "job-new"}


# ---------------------------------------------------------------------------
# _semver_tuple
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,expected", [
    ("1.1.3", (1, 1, 3)),
    ("2.0.1", (2, 0, 1)),
    ("2.0.0", (2, 0, 0)),
    ("1.1.2", (1, 1, 2)),
    ("2", (2, 0, 0)),
    ("2.1", (2, 1, 0)),
    ("", (0, 0, 0)),
    (None, (0, 0, 0)),
])
def test_semver_tuple(devices, version, expected):
    assert devices._semver_tuple(version) == expected


def test_semver_ordering(devices):
    st = devices._semver_tuple
    assert st("2.0.1") > st("1.1.3")
    assert st("2.0.0") > st("1.1.3")
    assert st("1.1.3") == st("1.1.3")
    assert st("1.1.2") < st("1.1.3")


# ---------------------------------------------------------------------------
# _cap_secure_tunneling_version
# ---------------------------------------------------------------------------

def test_cap_jp5_blocks_2x(devices):
    """JP5 must never receive SecureTunneling >= 2.0.0."""
    assert devices._cap_secure_tunneling_version("2.0.1", "arm64_jp5") == "1.1.3"
    assert devices._cap_secure_tunneling_version("2.0.0", "arm64_jp5") == "1.1.3"
    assert devices._cap_secure_tunneling_version("3.5.0", "arm64_jp5") == "1.1.3"


def test_cap_jp5_passes_compatible(devices):
    """A version at or below the cap is left unchanged on JP5."""
    assert devices._cap_secure_tunneling_version("1.1.3", "arm64_jp5") == "1.1.3"
    assert devices._cap_secure_tunneling_version("1.1.2", "arm64_jp5") == "1.1.2"
    assert devices._cap_secure_tunneling_version("1.0.19", "arm64_jp5") == "1.0.19"


@pytest.mark.parametrize("arch", ["arm64_jp6", "x86_64", "arm64_jp4", None])
def test_cap_non_jp5_untouched(devices, arch):
    """Only JP5 is capped; every other arch (and unknown) passes through."""
    assert devices._cap_secure_tunneling_version("2.0.1", arch) == "2.0.1"


# ---------------------------------------------------------------------------
# _device_local_server_arch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("component_name,expected_arch", [
    ("aws.edgeml.dda.LocalServer.arm64JP5", "arm64_jp5"),
    ("aws.edgeml.dda.LocalServer.arm64JP6", "arm64_jp6"),
    ("aws.edgeml.dda.LocalServer.x86_64", "x86_64"),
])
def test_device_arch_detection(devices, component_name, expected_arch):
    gg = _FakeGreengrass(local_server_name=component_name)
    assert devices._device_local_server_arch(gg, "thing-x") == expected_arch


def test_device_arch_none_when_no_local_server(devices):
    gg = _FakeGreengrass(local_server_name=None)
    assert devices._device_local_server_arch(gg, "thing-x") is None


# ---------------------------------------------------------------------------
# set_ssh_tunnel end-to-end capping
# ---------------------------------------------------------------------------

def _wire_ssh_tunnel(devices, monkeypatch, gg):
    """Stub the use-case context, client factory, and audit logging so
    set_ssh_tunnel can run against the fake greengrass client."""
    monkeypatch.setattr(devices, "_resolve_usecase_context", lambda *a, **k: (
        {"usecase": {}, "usecase_id": "uc-1", "credentials": {},
         "region": "us-east-1", "account_id": "123456789012"}, None))
    monkeypatch.setattr(devices, "create_boto3_client", lambda *a, **k: gg)
    monkeypatch.setattr(devices, "_latest_secure_tunneling_version",
                        lambda *a, **k: "2.0.1")
    monkeypatch.setattr(devices, "log_audit_event", lambda *a, **k: None)


def test_set_ssh_tunnel_caps_on_jp5(devices, monkeypatch):
    """Enabling SSH on a JP5 device deploys the capped 1.1.3, not latest."""
    gg = _FakeGreengrass(local_server_name="aws.edgeml.dda.LocalServer.arm64JP5")
    _wire_ssh_tunnel(devices, monkeypatch, gg)

    resp = devices.set_ssh_tunnel(
        "jp5-device", {"user_id": "u1"}, {"usecase_id": "uc-1"}, {"enabled": True})

    assert resp["statusCode"] == 200
    comps = gg.created[-1]["components"]
    assert comps["aws.greengrass.SecureTunneling"]["componentVersion"] == "1.1.3"


def test_set_ssh_tunnel_latest_on_jp6(devices, monkeypatch):
    """A JP6 device is unaffected and receives the public latest version."""
    gg = _FakeGreengrass(local_server_name="aws.edgeml.dda.LocalServer.arm64JP6")
    _wire_ssh_tunnel(devices, monkeypatch, gg)

    resp = devices.set_ssh_tunnel(
        "jp6-device", {"user_id": "u1"}, {"usecase_id": "uc-1"}, {"enabled": True})

    assert resp["statusCode"] == 200
    comps = gg.created[-1]["components"]
    assert comps["aws.greengrass.SecureTunneling"]["componentVersion"] == "2.0.1"
