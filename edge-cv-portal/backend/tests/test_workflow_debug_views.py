"""``workflow_debug.views.variant_notice`` (workflow-debug-mode design §5.6):
fires for ``x86_64_nvidia`` with ``publishFailures > 0``; not for ``x86_64``,
``arm64_jp5`` or a missing record.
"""
import pytest

from workflow_debug import views


def test_fires_for_the_unsupported_variant_with_publish_failures():
    notice = views.variant_notice("x86_64_nvidia", 3)
    assert notice == views.VARIANT_NOTICE
    assert "mqttproxy:2" in notice and "recipe-amd64-nvidia.yaml" in notice and "x86_64_nvidia" in notice


@pytest.mark.parametrize("architecture", ["x86_64", "arm64_jp5", "arm64_jp6", "arm64_jp7", "arm64_cpu", None, ""])
def test_other_architectures_and_a_missing_record_do_not_fire(architecture):
    assert views.variant_notice(architecture, 3) is None


@pytest.mark.parametrize("failures", [0, None, "0", -1, "x"])
def test_no_publish_failures_does_not_fire(failures):
    assert views.variant_notice("x86_64_nvidia", failures) is None


def test_string_counters_from_a_reported_document_are_accepted():
    assert views.variant_notice("x86_64_nvidia", "12") == views.VARIANT_NOTICE


def test_the_device_record_vocabulary_is_the_portals():
    assert views.UNSUPPORTED_TARGET_ARCHITECTURE == "x86_64_nvidia"
