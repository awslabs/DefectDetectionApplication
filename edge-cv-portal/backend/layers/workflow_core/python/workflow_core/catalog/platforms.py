"""
Plugin build platform table (custom-node-source-lifecycle, Requirements 5.10,
7.4).

One entry per Target_Architecture the Plugin_Build_Service can build for,
describing the toolchain of that architecture's ``dda-plugin-build`` image
(``edge-cv-portal/plugin-build-images/Dockerfile.<arch>``). The table is
the single source of truth consumed by:

- ``plugin_importer.PLATFORM_GSTREAMER_VERSIONS`` (the advisory
  per-platform GStreamer requirement check of repository imports),
- the Code_Assist_Generator's Node_Designer system prompt (so build
  diagnoses account for the failing target's OS release, GStreamer
  version, and build tooling), and
- the frontend labels served through the node catalog route.

Pure data: no I/O, importable everywhere workflow_core is.
"""
from typing import Dict, List

from .models import (
    ARCH_ARM64_CPU,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_ARM64_JP7,
    ARCH_X86_64,
    ARCH_X86_64_NVIDIA,
)

#: arch -> {label, os, gstreamer, meson, compiler, notes}
BUILD_PLATFORMS: Dict[str, Dict[str, str]] = {
    ARCH_X86_64: {
        "label": "x86_64",
        "os": "Ubuntu 22.04",
        "gstreamer": "1.20",
        "meson": "0.61",
        "compiler": "gcc 11",
        "notes": "CPU-only build image; the cloud test sandbox and the "
                 "Plugin_Simulator execute this artifact.",
    },
    ARCH_X86_64_NVIDIA: {
        "label": "x86_64 (NVIDIA GPU)",
        "os": "Ubuntu 22.04 + CUDA toolkit",
        "gstreamer": "1.20",
        "meson": "0.61",
        "compiler": "gcc 11",
        "notes": "Same base as x86_64 plus the CUDA toolkit and NVIDIA "
                 "GStreamer runtime headers.",
    },
    ARCH_ARM64_CPU: {
        "label": "arm64 CPU",
        "os": "Ubuntu 20.04",
        "gstreamer": "1.16",
        "meson": "1.4 (pip meson in the image)",
        "compiler": "gcc 9",
        "notes": "Generic non-Jetson arm64 host (e.g. AWS Graviton): no "
                 "NVIDIA stack; no meson subproject fallback for newer "
                 "GStreamer.",
    },
    ARCH_ARM64_JP5: {
        "label": "arm64 JetPack 5",
        "os": "L4T r35 / Ubuntu 20.04",
        "gstreamer": "1.16",
        "meson": "0.53",
        "compiler": "gcc 9",
        "notes": "No meson subproject fallback for newer GStreamer; "
                 "DeepStream 6.x SDK.",
    },
    ARCH_ARM64_JP6: {
        "label": "arm64 JetPack 6",
        "os": "L4T r36 / Ubuntu 22.04",
        "gstreamer": "1.20",
        "meson": "0.61",
        "compiler": "gcc 11",
        "notes": "DeepStream 7.x SDK; meson subproject fallback available.",
    },
    ARCH_ARM64_JP7: {
        "label": "arm64 JetPack 7",
        "os": "Ubuntu 24.04 + CUDA 13",
        "gstreamer": "1.24",
        "meson": "1.3",
        "compiler": "gcc 13",
        "notes": "Jetson Thor (r38.x); CUDA 13 base image shared with the "
                 "JetPack 7 LocalServer; meson subproject fallback available.",
    },
}

#: Platforms whose build image toolchain (Ubuntu 22.04 or newer: modern
#: meson, glib, and headers) can build a newer GStreamer via meson's
#: subproject fallback when the source requires more than the platform
#: ships. Observed in production: gst-plugins-good main (requires
#: GStreamer >= 1.24) builds fine on the Ubuntu 22.04 platforms via the
#: fallback, while the Ubuntu 20.04 platforms (arm64_cpu, arm64_jp5) fail
#: with an obscure meson subproject error.
PLATFORMS_WITH_SUBPROJECT_FALLBACK = frozenset(
    {ARCH_X86_64, ARCH_X86_64_NVIDIA, ARCH_ARM64_JP6, ARCH_ARM64_JP7})


def platform_gstreamer_versions() -> Dict[str, str]:
    """arch -> GStreamer release series shipped by the build platform."""
    return {arch: entry["gstreamer"] for arch, entry in BUILD_PLATFORMS.items()}


def platform_labels() -> Dict[str, str]:
    """arch -> human-readable platform label (kept in line with the
    frontend's ARCHITECTURE_LABELS)."""
    return {arch: entry["label"] for arch, entry in BUILD_PLATFORMS.items()}


def describe_build_platforms(architectures=None) -> List[str]:
    """One plain-text line per platform for prompts and documentation:
    ``- arm64_jp7: Ubuntu 24.04 + CUDA 13, GStreamer 1.24, meson 1.3, gcc 13``."""
    selected = architectures if architectures is not None else list(BUILD_PLATFORMS)
    lines: List[str] = []
    for arch in selected:
        entry = BUILD_PLATFORMS.get(arch)
        if not entry:
            continue
        lines.append(
            f"- {arch}: {entry['os']}, GStreamer {entry['gstreamer']}, "
            f"meson {entry['meson']}, {entry['compiler']}. {entry['notes']}")
    return lines
