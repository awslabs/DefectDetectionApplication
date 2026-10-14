"""Property test for the Plugin_Importer buildability scan (task 4.2).

**Feature: custom-node-designer, Property 4: Import buildability scan matches source-tree construction**

For all synthetic source trees generated with or without a GStreamer
plugin build definition (meson/autotools plugin target, or prebuilt
.so), the Plugin_Importer's buildability scan reports buildable if and
only if the tree was constructed with one, and reports a non-empty
finding when unbuildable.

**Validates: Requirements 4.5**

The scan under test (`scan_buildability`) is a pure function over a
{relative_path: content-or-None} source-tree mapping, so it is
exercised directly with no AWS involvement. The module is imported
through the shared moto-backed session fixture only so the real
`shared_utils` layer (not a test fake) backs the import.

Construction is the reference model: buildable trees are planted with
one known build definition; unbuildable trees are generated from
alphabets and extensions that cannot form a prebuilt `.so` name, a
meson plugin-target declaration, or an autotools GStreamer reference.
"""

from __future__ import annotations

import posixpath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="session")
def importer(aws_stack):
    """The real plugin_importer module, imported via the session stack."""
    return aws_stack.plugin_importer


# ---------------------------------------------------------------------------
# Noise: source-tree content guaranteed free of any build definition.
#
# The requirement (4.5) makes a tree buildable through exactly three
# doors: a prebuilt `.so` binary, a meson.build declaring a GStreamer
# plugin library target, or a configure.ac/.in referencing GStreamer.
# Noise closes all three by construction:
#   - file names never end in ".so" (safe extensions, dot-free stems);
#   - build-definition file content is drawn from an alphabet with no
#     letters besides x/y/z, so the tokens `library(`, `shared_*`,
#     `gstreamer-1.0`, `gst_*`, `gst-plugin`, `GST_PLUGIN`, `AG_GST_`
#     cannot occur (or the content is None, as for every file whose
#     content the scan never fetches).
# ---------------------------------------------------------------------------

_DEFINITION_NAMES = ("meson.build", "configure.ac", "configure.in")

_SEGMENT = st.text(alphabet="abcdefghijklmnop", min_size=1, max_size=8)
_SAFE_EXT = st.sampled_from(["", ".c", ".h", ".txt", ".md", ".py", ".sh", ".json"])

#: Text that cannot spell any build-definition trigger token.
_INERT_TEXT = st.text(alphabet="xyz012 \n\t#()'=,.-", max_size=120)

_dirs = st.lists(_SEGMENT, min_size=0, max_size=3)


def _join(dirs, basename):
    return "/".join(list(dirs) + [basename])


#: An ordinary source file: never a definition name, never *.so.
_plain_file = st.builds(
    lambda dirs, stem, ext: (_join(dirs, stem + ext), None),
    _dirs, _SEGMENT, _SAFE_EXT,
).filter(lambda kv: posixpath.basename(kv[0]) not in _DEFINITION_NAMES)

#: A build-definition file whose content declares nothing (inert text
#: or None): present in the tree but never making it buildable.
_inert_definition_file = st.builds(
    lambda dirs, name, content: (_join(dirs, name), content),
    _dirs, st.sampled_from(_DEFINITION_NAMES),
    st.one_of(st.none(), _INERT_TEXT),
)

_noise_tree = st.lists(
    st.one_of(_plain_file, _inert_definition_file), max_size=8,
).map(dict)


# ---------------------------------------------------------------------------
# Planted build definitions: each generator returns
# (kind, relative_path, content) for one known-buildable definition.
# ---------------------------------------------------------------------------

_MESON_TARGETS = ("library", "shared_library", "shared_module")
_MESON_GST_REFS = (
    "dependency('gstreamer-1.0')",
    "# needs gst-plugin support",
    "gst_dep",
)
_AUTOTOOLS_GST_REFS = (
    "PKG_CHECK_MODULES(GST, gstreamer-1.0 >= 1.20)",
    "GST_PLUGIN_LDFLAGS='-module -avoid-version'",
    "AG_GST_INIT",
)

_planted_prebuilt = st.builds(
    lambda dirs, stem: ("prebuilt", _join(dirs, "libgst" + stem + ".so"), None),
    _dirs, _SEGMENT,
)

_planted_meson = st.builds(
    lambda dirs, target, gap, gst_ref, filler: (
        "meson",
        _join(dirs, "meson.build"),
        f"{filler}\ngst = {gst_ref}\n"
        f"{target}{gap}('myplugin', 'plugin.c', dependencies: [gst])\n",
    ),
    _dirs, st.sampled_from(_MESON_TARGETS), st.sampled_from(["", " ", "  "]),
    st.sampled_from(_MESON_GST_REFS), _INERT_TEXT,
)

_planted_autotools = st.builds(
    lambda dirs, name, gst_ref, filler: (
        "autotools", _join(dirs, name), f"{filler}\n{gst_ref}\n",
    ),
    _dirs, st.sampled_from(("configure.ac", "configure.in")),
    st.sampled_from(_AUTOTOOLS_GST_REFS), _INERT_TEXT,
)

_planted_definition = st.one_of(
    _planted_prebuilt, _planted_meson, _planted_autotools)


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(noise=_noise_tree, planted=_planted_definition)
def test_buildable_tree_is_reported_buildable(importer, noise, planted):
    """**Feature: custom-node-designer, Property 4: Import buildability scan matches source-tree construction**

    For all source trees planted with a known GStreamer plugin build
    definition (prebuilt .so, meson plugin target, or autotools
    GStreamer reference) amid inert noise files, the scan reports
    buildable, of the planted kind, with the planted file as evidence.

    **Validates: Requirements 4.5**
    """
    kind, path, content = planted
    tree = dict(noise)
    tree[path] = content

    scan = importer.scan_buildability(tree)

    assert scan["buildable"] is True
    assert scan["kind"] == kind
    assert scan["evidence"] == [path]
    assert scan["finding"] == ""


@settings(max_examples=25, deadline=None)
@given(noise=_noise_tree)
def test_unbuildable_tree_is_reported_unbuildable_with_finding(importer, noise):
    """**Feature: custom-node-designer, Property 4: Import buildability scan matches source-tree construction**

    For all source trees constructed with no GStreamer plugin build
    definition (no .so, no declaring meson.build, no GStreamer-
    referencing configure.ac/.in), the scan reports unbuildable with
    no evidence and a non-empty finding to report to the user (4.5).

    **Validates: Requirements 4.5**
    """
    scan = importer.scan_buildability(noise)

    assert scan["buildable"] is False
    assert scan["kind"] is None
    assert scan["evidence"] == []
    assert isinstance(scan["finding"], str) and scan["finding"]
