# -*- coding: utf-8 -*-
"""Backend deployment-gate verification (task 4.6) for
onnx-jetson-publish-packaging.

**Property 8: Fix Checking — backend deployment gates ignore ONNX model
names.**

Per-JetPack ONNX model components are named
`model-{safe}-onnx-jetson-xavier-jp{N}` — they do NOT start with
`model-vllm-`, so `deployments.py::collect_vllm_component_manifests`
produces NO manifest for them: the vLLM architecture gate never activates
on them and a 409 `VLLM_ARCH_UNSUPPORTED` is impossible for a deployment
whose only model components are ONNX names. And
`local_server_component_arch` keys off the installed LocalServer component
name only (`aws.edgeml.dda.LocalServer.*` prefix), so model component names
never influence the LocalServer minimum-version gating.

This suite is VERIFICATION ONLY (design Property 8): no production change
was made for it, and none is expected — if a property here fails, that is a
finding to report, not to silently fix.

Generators are constrained to the Per_JetPack_ONNX_Component input space:
safe vision `model-{slug}` bases (no `vllm-` prefix — the `model-vllm-`
namespace belongs to the vLLM publish path and is out of scope here).

Hypothesis settings come from the conftest-registered profiles
(`portal-fast`/`ci`) — no hardcoded `max_examples`.

Run (needs the tests-directory conftest — no `--noconftest`), from
edge-cv-portal/backend/tests with the /home/ubuntu/.venvs/dda-portal-tests
venv:
    python3 -m pytest test_onnx_jetson_deployment_gates_properties.py \
      -q -p no:cacheprovider

**Validates: Requirements 2.13**
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"

#: Safe vision model-name slugs — never 'vllm-'-prefixed (see module
#: docstring).
_slugs = st.text(alphabet=_ALNUM + "-", min_size=1, max_size=16).filter(
    lambda s: not s.startswith("-") and not s.endswith("-")
    and "--" not in s and not s.startswith("vllm-"))

#: Per-JetPack ONNX component names: model-{safe}-onnx-jetson-xavier-jp{N}.
onnx_component_names = st.builds(
    lambda slug, major: f"model-{slug}-onnx-jetson-xavier-jp{major}",
    _slugs, st.sampled_from(["5", "6", "7"]))

onnx_component_sets = st.dictionaries(
    keys=onnx_component_names,
    values=st.sampled_from(["1.0.0", "2.0.0", "1.3.7"]),
    min_size=1, max_size=4)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """deployments imported inside the moto mock so its module-level boto3
    clients are intercepted. No training-jobs table is needed: nothing in
    these properties may ever reach the GSI lookup — that is the point."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@settings(deadline=None)
@given(components=onnx_component_sets)
def test_property_8_vllm_gate_ignores_onnx_model_names(
        deployments, components):
    """**Property 8: Fix Checking — the vLLM architecture gate ignores
    per-JetPack ONNX model names.**

    For any generated set of Per_JetPack_ONNX_Component names (any JetPack
    major, any versions), `collect_vllm_component_manifests` produces NO
    manifest — the names carry no `model-vllm-` prefix, so the gate never
    activates, and with zero manifests `check_vllm_deployment_gate` returns
    None without touching any device: a 409 `VLLM_ARCH_UNSUPPORTED` is
    impossible for these components.

    # Validates: Requirements 2.13
    """
    for name in components:
        assert not name.startswith(
            deployments.VLLM_MODEL_COMPONENT_PREFIX), (
            f"generator produced a vLLM-prefixed name: {name}")

    manifests = deployments.collect_vllm_component_manifests(components)
    assert manifests == {}, (
        f"per-JetPack ONNX model names must produce NO vLLM gate "
        f"manifests; got {manifests!r} for {sorted(components)}")

    # Zero manifests -> the gate contributes zero findings and returns
    # None before any device lookup: no 409 VLLM_ARCH_UNSUPPORTED path.
    assert deployments.check_vllm_deployment_gate(
        manifests, ["any-core-device"]) is None


@settings(deadline=None)
@given(name=onnx_component_names)
def test_property_8_local_server_arch_unaffected_by_onnx_model_names(
        deployments, name):
    """**Property 8: Fix Checking — LocalServer min-version gating keys off
    the installed LocalServer name only.**

    For any Per_JetPack_ONNX_Component name,
    `local_server_component_arch` returns None (the name lacks the
    `aws.edgeml.dda.LocalServer` prefix), so a model component name can
    never masquerade as a LocalServer variant in the per-arch
    minimum-version gate; the real LocalServer variants keep resolving to
    their own arch ids regardless of the ONNX names being deployed.

    # Validates: Requirements 2.13
    """
    assert deployments.local_server_component_arch(name) is None, (
        f"model component name {name!r} must never resolve to a "
        f"LocalServer arch lineage")

    # The installed-LocalServer resolution itself is untouched by any model
    # component name: the JetPack variants keep their own lineages.
    prefix = "aws.edgeml.dda.LocalServer."
    assert deployments.local_server_component_arch(
        prefix + "arm64JP5") == "arm64_jp5"
    assert deployments.local_server_component_arch(
        prefix + "arm64JP6") == "arm64_jp6"
    assert deployments.local_server_component_arch(
        prefix + "arm64JP7") == "arm64_jp7"
