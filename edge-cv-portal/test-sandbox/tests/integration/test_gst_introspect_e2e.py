"""Containerized integration test for ``dda-gst-introspect``
(gst-parameter-prepopulation task 4.6, Requirements 1.2, 1.3).

Runs the real introspection script (``plugin-build-images/
dda-gst-introspect``, the one ``Dockerfile.x86_64`` ships at
``/usr/local/bin``) inside the sandbox container image, which carries the
same GStreamer + Python GI runtime the x86_64 plugin-build image uses.
Reuses the session fixtures from ``conftest.py`` (Docker prerequisite;
skips cleanly when missing; ``SANDBOX_IT_IMAGE`` selects a prebuilt
image).

The scanned plugin is the distro's stock ``videofilter`` plugin
(``libgstvideofilter.so`` from gstreamer1.0-plugins-good), whose
``videoflip`` element has a GEnum property (``method`` →
``GstVideoFlipMethod``) plus the ``GstObject`` base-class properties
(``name``, ``parent``) — exactly the metadata Requirements 1.2/1.3 need
captured. Because the script only reports factories whose plugin file
lives under the scan directory (realpath prefix check), the ``.so`` is
*copied* (not symlinked) into a scratch scan dir inside the container and
the system copy is removed first, so the registry can only associate the
``videoflip`` factory with the scan-dir copy.

The script itself is streamed over stdin into the container (no bind
mount: snap-confined Docker installations cannot read arbitrary host
paths), and stdout is captured separately from stderr — the report
contract is that stdout carries exactly one JSON document.
"""

import json
import os
import re
import subprocess
import importlib.util

import pytest

pytestmark = pytest.mark.integration

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EDGE_CV_PORTAL_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))

#: The script under test, exactly as shipped in the x86_64 build image.
_INTROSPECT_SCRIPT = os.path.join(
    _EDGE_CV_PORTAL_DIR, "plugin-build-images", "dda-gst-introspect")

#: The pure backend module whose parse_report must accept the produced
#: report (the same validation plugin_builds.py and the serving route run).
_GST_PROPERTIES_MODULE = os.path.join(
    _EDGE_CV_PORTAL_DIR, "backend", "functions", "gst_properties.py")

#: Arch-globbed path of the stock plugins-good videofilter plugin inside
#: the image; it registers the ``videoflip`` element (GEnum ``method``).
_IMAGE_PLUGIN_GLOB = "/usr/lib/*/gstreamer-1.0/libgstvideofilter.so"

#: In-container preparation: receive the script over stdin, copy the stock
#: plugin into a scratch scan dir (a real copy — the script resolves
#: realpath, so a symlink pointing outside the scan dir would be filtered
#: out), remove the system copy so the factory can only be attributed to
#: the scan-dir file, then run the introspection.
_RUN_INTROSPECTION = """\
set -e
cat > /tmp/dda-gst-introspect
mkdir -p /tmp/gst-scan
cp {glob} /tmp/gst-scan/
rm -f {glob}
exec python3 /tmp/dda-gst-introspect /tmp/gst-scan libgstvideofilter.so
""".format(glob=_IMAGE_PLUGIN_GLOB)


def _load_gst_properties():
    """Import backend/functions/gst_properties.py by file path (pure
    module, no boto3), without touching sys.path for other tests."""
    spec = importlib.util.spec_from_file_location(
        "gst_properties_it", _GST_PROPERTIES_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def introspection_report(sandbox_image):
    """The Introspection_Report JSON document produced by running the real
    ``dda-gst-introspect`` against the stock videofilter plugin inside the
    sandbox container image (one container run shared by the tests)."""
    with open(_INTROSPECT_SCRIPT, "rb") as handle:
        script_bytes = handle.read()

    completed = subprocess.run(
        ["docker", "run", "--rm", "-i", "--entrypoint", "sh",
         sandbox_image, "-c", _RUN_INTROSPECTION],
        input=script_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=300)

    stderr = completed.stderr.decode("utf-8", errors="replace")
    # The script's contract: content failures never exit non-zero; a
    # non-zero exit means the container plumbing itself broke.
    assert completed.returncode == 0, (
        "introspection container exited {0}; stderr:\n{1}".format(
            completed.returncode, stderr))

    stdout = completed.stdout.decode("utf-8")
    # stdout carries exactly one JSON document (diagnostics go to stderr).
    document = json.loads(stdout)
    assert isinstance(document, dict), "report must be a JSON object"
    return document


def _element_by_factory(document, factory):
    matches = [element for element in document["elements"]
               if element["factory"] == factory]
    assert matches, "factory {0!r} not in report (got: {1})".format(
        factory, [element["factory"] for element in document["elements"]])
    return matches[0]


def _properties_by_name(element):
    return {prop["name"]: prop for prop in element["properties"]}


def test_report_shape_enum_capture_and_blurbs(introspection_report):
    """The produced report has the version-1 captured shape, and the
    videoflip element's GEnum ``method`` property carries its enum values
    with nicks, a default resolved to a nick, and its blurb
    (Requirement 1.2)."""
    document = introspection_report

    # --- report shape (version 1, captured) ---
    assert document["reportVersion"] == 1
    assert document["status"] == "captured"
    assert document["message"] is None
    assert re.match(r"^\d+\.\d+\.\d+$", document["gstVersion"])
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                    document["capturedAt"])
    assert isinstance(document["elements"], list) and document["elements"]
    for element in document["elements"]:
        assert set(element) == {"factory", "elementGType",
                                "instantiationError", "properties"}
        assert isinstance(element["factory"], str) and element["factory"]
        assert isinstance(element["properties"], list)

    # --- the videoflip element instantiated for real ---
    videoflip = _element_by_factory(document, "videoflip")
    assert videoflip["instantiationError"] is None
    assert videoflip["elementGType"] == "GstVideoFlip"
    props = _properties_by_name(videoflip)

    # --- GEnum capture: method -> GstVideoFlipMethod (1.2) ---
    method = props["method"]
    assert method["gtype"] == "GstVideoFlipMethod"
    assert method["writable"] is True
    enum_values = method["enumValues"]
    assert isinstance(enum_values, list) and enum_values
    for entry in enum_values:
        assert isinstance(entry["value"], int)
        assert isinstance(entry["nick"], str) and entry["nick"]
    nicks = {entry["nick"] for entry in enum_values}
    assert {"none", "rotate-180"} <= nicks
    # The default is resolved to a nick, not left as a raw integer.
    assert method["default"] in nicks
    # No numeric range on a GEnum property.
    assert method["min"] is None and method["max"] is None

    # --- blurbs are recorded (1.2) ---
    assert isinstance(method["blurb"], str) and method["blurb"].strip()
    assert isinstance(props["name"]["blurb"], str) and props["name"]["blurb"]


def test_base_class_owners_recorded_and_report_parses(introspection_report):
    """Base-class properties carry the declaring class as owner —
    ``name``/``parent`` are owned by ``GstObject``, not the element's own
    GType (Requirement 1.3) — and the produced document parses cleanly
    through the backend's ``parse_report`` (the same validation the build
    recorder and the serving route apply)."""
    document = introspection_report
    videoflip = _element_by_factory(document, "videoflip")
    props = _properties_by_name(videoflip)

    # --- base-class ownership ground truth (1.3) ---
    for base_prop in ("name", "parent"):
        assert props[base_prop]["owner"] == "GstObject"
        assert props[base_prop]["owner"] != videoflip["elementGType"]
    # The element's own property is owned by its own GType.
    assert props["method"]["owner"] == videoflip["elementGType"]

    # --- the report is exactly what the backend accepts ---
    gst_properties = _load_gst_properties()
    report = gst_properties.parse_report(document)
    assert report.status == gst_properties.STATUS_CAPTURED
    parsed_videoflip = next(element for element in report.elements
                            if element.factory == "videoflip")
    parsed_props = {prop.name: prop for prop in parsed_videoflip.properties}
    assert parsed_props["method"].enum_values, \
        "parsed report keeps the GEnum values"
    assert parsed_props["name"].owner == "GstObject"
