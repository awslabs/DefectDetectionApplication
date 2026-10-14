"""Upstream Plugin_Set_Classification for public GStreamer plugins.

Pure derivation of the upstream good/bad/ugly quality taxonomy from a
module's identity: the official plugin-set module names
(``gst-plugins-good``, ``gst-plugins-bad``, ``gst-plugins-ugly``) and
their known freedesktop.org repository locations. Anything else —
including arbitrary public repositories — is never guessed into an
official set and classifies as ``unclassified`` (Requirements 15.3,
15.4).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

#: The four Plugin_Set_Classification values (upstream's own taxonomy).
CLASSIFICATION_GOOD = "good"
CLASSIFICATION_BAD = "bad"
CLASSIFICATION_UGLY = "ugly"
CLASSIFICATION_UNCLASSIFIED = "unclassified"

CLASSIFICATIONS = (
    CLASSIFICATION_GOOD,
    CLASSIFICATION_BAD,
    CLASSIFICATION_UGLY,
    CLASSIFICATION_UNCLASSIFIED,
)

#: Fixed plain-language explanation for each classification value,
#: presented verbatim alongside the classification (Requirement 15.3).
EXPLANATIONS = {
    CLASSIFICATION_GOOD: (
        "good indicates a well-maintained, well-tested, properly "
        "licensed plugin set"
    ),
    CLASSIFICATION_BAD: (
        "bad indicates a plugin set lacking upstream review, testing, "
        "or active maintenance"
    ),
    CLASSIFICATION_UGLY: (
        "ugly indicates a plugin set of good quality that carries "
        "licensing or distribution concerns"
    ),
    CLASSIFICATION_UNCLASSIFIED: (
        "unclassified indicates a plugin outside the official GStreamer "
        "plugin sets that warrants the highest caution"
    ),
}

#: Official plugin-set module names -> classification.
_OFFICIAL_SET_MODULES = {
    "gst-plugins-good": CLASSIFICATION_GOOD,
    "gst-plugins-bad": CLASSIFICATION_BAD,
    "gst-plugins-ugly": CLASSIFICATION_UGLY,
}

# Known freedesktop.org hosts that publish the official plugin sets:
# the GitLab instance (per-set repos and the monorepo subprojects), the
# historical cgit/anongit mirrors, and the gstreamer.freedesktop.org
# source-release tree.
_GITLAB_HOST = "gitlab.freedesktop.org"
_LEGACY_GIT_HOSTS = frozenset({"cgit.freedesktop.org", "anongit.freedesktop.org"})
_RELEASE_HOST = "gstreamer.freedesktop.org"
_URL_SCHEMES = frozenset({"http", "https", "git"})


def _path_segments(path: str) -> list:
    """URL path split into segments with any ``.git`` suffix stripped."""
    segments = []
    for raw in path.split("/"):
        if not raw:
            continue
        if raw.endswith(".git"):
            raw = raw[: -len(".git")]
        segments.append(raw)
    return segments


def _classify_repo_url(repo_url: str) -> str:
    """Classification for a repository URL, ``unclassified`` unless the
    URL is a known freedesktop.org location of an official plugin set."""
    try:
        parts = urlsplit(repo_url.strip())
    except ValueError:
        return CLASSIFICATION_UNCLASSIFIED

    if parts.scheme.lower() not in _URL_SCHEMES:
        return CLASSIFICATION_UNCLASSIFIED

    host = (parts.hostname or "").lower()
    segments = _path_segments(parts.path)

    if host == _GITLAB_HOST:
        # https://gitlab.freedesktop.org/gstreamer/gst-plugins-good[.git]
        # and monorepo subproject paths such as
        # https://gitlab.freedesktop.org/gstreamer/gstreamer/-/tree/
        #     main/subprojects/gst-plugins-good
        if segments and segments[0] == "gstreamer":
            for segment in segments[1:]:
                if segment in _OFFICIAL_SET_MODULES:
                    return _OFFICIAL_SET_MODULES[segment]
        return CLASSIFICATION_UNCLASSIFIED

    if host in _LEGACY_GIT_HOSTS:
        # https://cgit.freedesktop.org/gstreamer/gst-plugins-good/
        # git://anongit.freedesktop.org/gstreamer/gst-plugins-good
        if (
            len(segments) >= 2
            and segments[0] == "gstreamer"
            and segments[1] in _OFFICIAL_SET_MODULES
        ):
            return _OFFICIAL_SET_MODULES[segments[1]]
        return CLASSIFICATION_UNCLASSIFIED

    if host == _RELEASE_HOST:
        # https://gstreamer.freedesktop.org/src/gst-plugins-good/...
        for segment in segments:
            if segment in _OFFICIAL_SET_MODULES:
                return _OFFICIAL_SET_MODULES[segment]
        return CLASSIFICATION_UNCLASSIFIED

    return CLASSIFICATION_UNCLASSIFIED


def classify_plugin_set(
    module_name: Optional[str], repo_url: Optional[str]
) -> str:
    """The Plugin_Set_Classification for a module.

    ``good``/``bad``/``ugly`` exactly when the module name is one of the
    official plugin-set module names or the repository URL is a known
    freedesktop.org location of an official set; ``unclassified``
    otherwise (Requirement 15.4). Pure and deterministic — no network
    access, no guessing.
    """
    if module_name:
        name = module_name.strip()
        if name in _OFFICIAL_SET_MODULES:
            return _OFFICIAL_SET_MODULES[name]

    if repo_url and repo_url.strip():
        return _classify_repo_url(repo_url)

    return CLASSIFICATION_UNCLASSIFIED
