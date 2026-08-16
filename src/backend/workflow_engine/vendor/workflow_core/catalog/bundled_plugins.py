"""Per-architecture manifest of GStreamer plugins bundled with LocalServer.

The Workflow_Compiler computes a compiled pipeline's external plugin
dependencies as the union of the catalog-declared dependencies of its
node mappings minus this bundled set for the target architecture
(Requirement 6.4). Entries prefixed ``python:`` are Python runtime
packages available in the LocalServer environment rather than GStreamer
plugins.
"""

from __future__ import annotations

from .models import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_ARM64_JP7,
    ARCH_SIM,
    ARCH_X86_64,
    ARCH_X86_64_NVIDIA,
)

# Core GStreamer plugins present in every LocalServer image (base, good,
# bad-but-shipped sets used by the existing pipeline_builder paths).
_GST_CORE = frozenset(
    {
        "coreelements",  # filesrc, filesink, fakesink, tee, queue, capsfilter
        "app",  # appsrc, appsink
        "videoconvertscale",  # videoconvert, videoscale
        "videocrop",  # videocrop
        "videofilter",  # videoflip
        "jpeg",  # jpegenc, jpegdec, jpegparse
        "png",  # pngdec, pngenc
        "bayer",  # bayer2rgb
        "video4linux2",  # v4l2src
        "multifile",  # multifilesrc (used by the sim harness)
    }
)

# DDA edgemlsdk plugins shipped inside every LocalServer component.
_DDA_ELEMENTS = frozenset(
    {
        "emltriton",
        "emlcapture",
        "emoutputevent",
        "emexifextract",
    }
)

# Python runtime packages available inside LocalServer (used by
# executor-level bindings).
_LOCALSERVER_PYTHON = frozenset(
    {
        "python:paho-mqtt",  # existing mqtt/ client
        "python:pillow",  # JP6 PNG staging path
    }
)

_COMMON = _GST_CORE | _DDA_ELEMENTS | _LOCALSERVER_PYTHON

#: arch -> frozenset of plugin names bundled with the LocalServer build
#: for that architecture. ``sim`` mirrors the x86_64 sandbox image.
#: ``x86_64_nvidia`` mirrors ``x86_64`` — the LocalServer amd64 GPU build
#: bundles the same base plugin set.
LOCALSERVER_BUNDLED_PLUGINS = {
    ARCH_X86_64: _COMMON,
    ARCH_X86_64_NVIDIA: _COMMON,
    ARCH_ARM64_JP4: _COMMON | frozenset({"nvvideo4linux2"}),
    ARCH_ARM64_JP5: _COMMON | frozenset({"nvvideo4linux2"}),
    ARCH_ARM64_JP6: _COMMON | frozenset({"nvvideo4linux2"}),
    # arm64_jp7 (Jetson Thor) does NOT get nvvideo4linux2: unlike the
    # l4t-jetpack-based JP4/JP5/JP6 images, Dockerfile.jp7 builds on the
    # nvcr CUDA 13 Ubuntu 24.04 base and installs only the upstream
    # gstreamer1.0-plugins-good/bad sets — no L4T GStreamer package that
    # would provide nvvideo4linux2. No catalog node type declares that
    # plugin today, so this choice is inert for dependency computation;
    # it is recorded here so a future JP7 node mapping that needs the
    # NVIDIA v4l2 elements is forced to confirm the image ships them.
    ARCH_ARM64_JP7: _COMMON,
    ARCH_SIM: _COMMON,
}


def bundled_plugins_for(arch: str) -> frozenset:
    """The LocalServer-bundled plugin set for ``arch``.

    Raises KeyError for unknown architectures so compiler callers surface
    unsupported-architecture errors instead of silently packaging
    everything.
    """
    return LOCALSERVER_BUNDLED_PLUGINS[arch]
