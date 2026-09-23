"""
Property tests for the Architecture_Addition helpers in plugin_builds.py
(custom-node-source-lifecycle tasks 2.3, 2.4, 2.5).

Properties 5 (monotonic union, against the moto-backed record), 6
(addition validation), and 7 (build-config planning).
"""
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from conftest import TEST_ENV
from test_plugin_builds import PluginBuildsEnv
from test_plugin_records import make_scaffold_declaration

CONFIGURED = sorted(json.loads(TEST_ENV["BUILD_PROJECTS_JSON"]))
ALL_ARCHES = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6", "arm64_jp7"]
DEEPSTREAM = {"arm64_jp4", "arm64_jp5", "arm64_jp6"}

_arch_lists = st.lists(st.sampled_from(CONFIGURED), min_size=1, max_size=3, unique=True)


@pytest.fixture(scope="module")
def builds(aws_stack):
    return aws_stack.plugin_builds


@pytest.fixture
def benv(aws_stack):
    return PluginBuildsEnv(aws_stack)


# ---------------------------------------------------------------- Property 5

class TestMonotonicUnion:
    """**Feature: custom-node-source-lifecycle, Property 5: Requested
    architectures are a monotonic union** — Validates: Requirements 6.4, 6.5"""

    @given(rounds=st.lists(st.tuples(st.sampled_from(["build", "add"]), _arch_lists),
                           min_size=1, max_size=5))
    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture,
                                     HealthCheck.too_slow])
    def test_union_grows_and_settles_over_the_whole_set(self, benv, rounds):
        usecase_id = benv.create_usecase()
        admin = benv.make_admin(usecase_id)
        plugin = benv.create_plugin(
            admin, usecase_id,
            declaration=make_scaffold_declaration(architectures=["x86_64"]))
        plugin_id = plugin["plugin_id"]
        module = benv.module

        expected = set()
        for kind, arches in rounds:
            item = benv.get_item(plugin_id, 1)
            # Settle every in-flight build so additions are not blocked.
            for arch, entry in (item.get("artifacts") or {}).items():
                if entry.get("buildStatus") in ("queued", "building"):
                    benv.stack.tables.plugin_records.update_item(
                        Key={"plugin_id": plugin_id, "version": 1},
                        UpdateExpression="SET artifacts.#a.buildStatus = :s",
                        ExpressionAttributeNames={"#a": arch},
                        ExpressionAttributeValues={":s": "failed"})
            if kind == "build":
                status, body = benv.post_build(admin, plugin_id, 1,
                                               {"architectures": arches})
                assert status == 202, body
                expected |= set(arches)
            else:
                new = [a for a in arches if a not in expected]
                event = benv._event("POST", "/plugins/{id}/versions/{v}/architectures",
                                    admin, plugin_id, 1, {"architectures": arches})
                response = module.handler(event, None)
                if new and len(new) == len(arches):
                    assert response["statusCode"] == 202, response["body"]
                    expected |= set(new)
                else:
                    # Already-requested arches are rejected wholesale; the
                    # set must not change.
                    assert response["statusCode"] == 400
            item = benv.get_item(plugin_id, 1)
            assert module.requested_architectures(item) == sorted(expected)

        item = benv.get_item(plugin_id, 1)
        artifacts = item.get("artifacts") or {}
        all_settled = all(
            (artifacts.get(a) or {}).get("buildStatus") in ("succeeded", "failed")
            for a in expected)
        assert module.builds_settled(item) == (all_settled and bool(expected))


# ---------------------------------------------------------------- Property 6

class TestAdditionValidation:
    """**Feature: custom-node-source-lifecycle, Property 6:
    Architecture_Addition validation** — Validates: Requirements 6.1, 6.8"""

    @given(requested=st.lists(st.one_of(st.sampled_from(ALL_ARCHES),
                                        st.sampled_from(["riscv64", "", "x86"])),
                              max_size=6),
           existing=st.lists(st.sampled_from(ALL_ARCHES), max_size=4, unique=True),
           registry=st.lists(st.sampled_from(ALL_ARCHES), max_size=6, unique=True),
           deepstream=st.booleans())
    def test_rejections_are_exact(self, builds, requested, existing, registry, deepstream):
        accepted, rejections = builds.validate_addition(
            requested, existing, registry, deepstream)
        expected_rejections = {}
        expected_accepted = []
        for arch in requested:
            if arch in expected_rejections or arch in expected_accepted:
                continue
            if arch not in ALL_ARCHES:
                expected_rejections[arch] = "unknown"
            elif arch in existing:
                expected_rejections[arch] = "already_requested"
            elif arch not in registry:
                expected_rejections[arch] = "unavailable"
            elif deepstream and arch not in DEEPSTREAM:
                expected_rejections[arch] = "deepstream_restricted"
            else:
                expected_accepted.append(arch)
        assert rejections == expected_rejections
        assert accepted == sorted(expected_accepted)
        acceptable = not rejections and bool(accepted)
        assert acceptable == (bool(requested) and not expected_rejections)


# ---------------------------------------------------------------- Property 7

class TestBuildConfigPlanning:
    """**Feature: custom-node-source-lifecycle, Property 7: Scaffold build
    configurations are rendered only where missing** — Validates:
    Requirements 6.3"""

    @given(existing=st.lists(st.sampled_from(ALL_ARCHES), min_size=1, max_size=3,
                             unique=True),
           added=st.lists(st.sampled_from(ALL_ARCHES), min_size=1, max_size=3,
                          unique=True),
           present_arches=st.lists(st.sampled_from(ALL_ARCHES), max_size=6, unique=True))
    def test_plans_exactly_the_missing_added_configs(self, builds, existing, added,
                                                     present_arches):
        from workflow_core.scaffold import build_config_path
        declaration = make_scaffold_declaration(architectures=existing)
        present = ["README.md", "plugin/frame_processing_hook.py"] + [
            build_config_path(a) for a in present_arches]
        planned, extended = builds.plan_build_configs(declaration, existing, added, present)

        expected_paths = {build_config_path(a) for a in added} - set(present)
        assert set(planned) == expected_paths
        assert all(content.strip() for content in planned.values())
        assert not (set(planned) & set(present))

        ordered = list(existing)
        for arch in added:
            if arch not in ordered:
                ordered.append(arch)
        assert extended["architectures"] == ordered
        # The original declaration is not mutated.
        assert declaration["architectures"] == existing
