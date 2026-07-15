"""Property test for plugin-set classification (task 1.5).

**Feature: custom-node-designer, Property 6: Plugin-set classification is exact**

For all plugin sources, ``classify_plugin_set`` returns good, bad, or
ugly exactly for modules belonging to the corresponding official
GStreamer plugin set (by module name or known repository location), and
unclassified for every other source — including arbitrary public
repository URLs; and every classification value has a non-empty
plain-language explanation.

**Validates: Requirements 15.1, 15.3, 15.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog.classification import (
    CLASSIFICATION_BAD,
    CLASSIFICATION_GOOD,
    CLASSIFICATION_UGLY,
    CLASSIFICATION_UNCLASSIFIED,
    CLASSIFICATIONS,
    EXPLANATIONS,
    classify_plugin_set,
)

# ---------------------------------------------------------------------------
# Ground truth: the official plugin-set module names and their expected
# classifications, restated here from the requirements (15.1) rather
# than imported, so the test cannot silently agree with a wrong table.
# ---------------------------------------------------------------------------

OFFICIAL_MODULES = {
    "gst-plugins-good": CLASSIFICATION_GOOD,
    "gst-plugins-bad": CLASSIFICATION_BAD,
    "gst-plugins-ugly": CLASSIFICATION_UGLY,
}

official_module_names = st.sampled_from(sorted(OFFICIAL_MODULES))

_padding = st.text(alphabet=" \t", max_size=2)

_segment_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_."

def _strip_git(segment):
    return segment[: -len(".git")] if segment.endswith(".git") else segment

#: URL path segments guaranteed not to be an official plugin-set name,
#: even after the classifier strips a ``.git`` suffix.
_safe_segment = st.text(
    alphabet=_segment_alphabet, min_size=1, max_size=25
).filter(lambda s: _strip_git(s) not in OFFICIAL_MODULES and s != ".git")


# ---------------------------------------------------------------------------
# Official sources: known freedesktop.org repository locations of the
# official plugin sets, in every supported URL shape.
# ---------------------------------------------------------------------------

@st.composite
def official_urls(draw):
    """(repo_url, expected_classification) for a known official location."""
    module = draw(official_module_names)
    expected = OFFICIAL_MODULES[module]
    shape = draw(st.sampled_from(["gitlab", "monorepo", "legacy", "release"]))
    if shape == "gitlab":
        suffix = draw(st.sampled_from(["", ".git", "/"]))
        url = "https://gitlab.freedesktop.org/gstreamer/%s%s" % (module, suffix)
    elif shape == "monorepo":
        branch = draw(st.sampled_from(["main", "1.22", "discontinued-for-monorepo"]))
        url = (
            "https://gitlab.freedesktop.org/gstreamer/gstreamer/-/tree/"
            "%s/subprojects/%s" % (branch, module)
        )
    elif shape == "legacy":
        scheme = draw(st.sampled_from(["https", "http", "git"]))
        host = draw(st.sampled_from(["cgit.freedesktop.org", "anongit.freedesktop.org"]))
        suffix = draw(st.sampled_from(["", "/"]))
        url = "%s://%s/gstreamer/%s%s" % (scheme, host, module, suffix)
    else:  # release
        url = "https://gstreamer.freedesktop.org/src/%s/" % module
    return url, expected


@st.composite
def official_sources(draw):
    """(module_name, repo_url, expected) where the source is official
    either by module name or by known repository location."""
    if draw(st.booleans()):
        # Official by module name (surrounding whitespace tolerated);
        # any accompanying URL — even a non-official one — must not
        # change the answer, since the name already identifies the set.
        module = draw(official_module_names)
        name = draw(_padding) + module + draw(_padding)
        url = draw(st.one_of(st.none(), st.just(""), arbitrary_urls()))
        return name, url, OFFICIAL_MODULES[module]
    # Official by known repository location; the module name, if any,
    # is a non-official one so classification must come from the URL.
    url, expected = draw(official_urls())
    name = draw(st.one_of(st.none(), st.just(""), arbitrary_module_names()))
    return name, url, expected


# ---------------------------------------------------------------------------
# Arbitrary (non-official) sources: never guessed into an official set.
# ---------------------------------------------------------------------------

def arbitrary_module_names():
    """Module names that are not an official plugin-set name."""
    return st.text(max_size=30).filter(
        lambda s: s.strip() not in OFFICIAL_MODULES
    )


@st.composite
def _other_host_urls(draw):
    """Public repositories on non-freedesktop hosts, including ones
    deliberately named after official plugin sets (Requirement 15.4)."""
    host = draw(st.sampled_from(
        ["github.com", "gitlab.com", "bitbucket.org", "example.com", "git.sr.ht"]
    ))
    segments = draw(st.lists(
        st.one_of(_safe_segment, official_module_names), min_size=1, max_size=4
    ))
    return "https://%s/%s" % (host, "/".join(segments))


@st.composite
def _freedesktop_non_official_urls(draw):
    """freedesktop.org hosts with paths that are not an official set."""
    shape = draw(st.sampled_from(["gitlab-other-ns", "gitlab-safe", "legacy", "release"]))
    if shape == "gitlab-other-ns":
        # Not the gstreamer namespace, even with an official-looking tail
        # (nor a segment that strips to it, e.g. "gstreamer.git").
        first = draw(_safe_segment.filter(lambda s: _strip_git(s) != "gstreamer"))
        rest = draw(st.lists(
            st.one_of(_safe_segment, official_module_names), max_size=3
        ))
        path = "/".join([first] + rest)
        return "https://gitlab.freedesktop.org/%s" % path
    if shape == "gitlab-safe":
        # gstreamer namespace but no official plugin-set segment.
        rest = draw(st.lists(_safe_segment, max_size=3))
        return "https://gitlab.freedesktop.org/%s" % "/".join(["gstreamer"] + rest)
    if shape == "legacy":
        host = draw(st.sampled_from(["cgit.freedesktop.org", "anongit.freedesktop.org"]))
        segments = draw(st.lists(_safe_segment, min_size=1, max_size=3))
        return "https://%s/%s" % (host, "/".join(segments))
    # release tree without an official plugin-set segment
    segments = draw(st.lists(_safe_segment, min_size=1, max_size=3))
    return "https://gstreamer.freedesktop.org/%s" % "/".join(segments)


@st.composite
def _wrong_scheme_urls(draw):
    """Official-looking paths behind unsupported URL schemes."""
    scheme = draw(st.sampled_from(["ftp", "ssh", "file", "svn"]))
    module = draw(official_module_names)
    return "%s://gitlab.freedesktop.org/gstreamer/%s" % (scheme, module)


#: Junk strings that are not URLs at all; excluding "freedesktop.org"
#: keeps them non-official by construction, not by circular checking.
_junk_urls = st.text(max_size=40).filter(lambda s: "freedesktop.org" not in s)

def arbitrary_urls():
    """Repository URLs that are not a known official location."""
    return st.one_of(
        _other_host_urls(),
        _freedesktop_non_official_urls(),
        _wrong_scheme_urls(),
        _junk_urls,
    )


@st.composite
def arbitrary_sources(draw):
    """(module_name, repo_url) with no official identity at all."""
    name = draw(st.one_of(st.none(), arbitrary_module_names()))
    url = draw(st.one_of(st.none(), arbitrary_urls()))
    return name, url


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

@settings(max_examples=25)
@given(source=official_sources())
def test_official_sources_classify_exactly(source):
    """**Feature: custom-node-designer, Property 6: Plugin-set classification is exact**

    Modules belonging to an official GStreamer plugin set — by module
    name or known repository location — classify to exactly that set.

    **Validates: Requirements 15.1, 15.4**
    """
    module_name, repo_url, expected = source
    assert classify_plugin_set(module_name, repo_url) == expected


@settings(max_examples=25)
@given(source=arbitrary_sources())
def test_arbitrary_sources_classify_unclassified(source):
    """**Feature: custom-node-designer, Property 6: Plugin-set classification is exact**

    Every other source — including arbitrary public repository URLs and
    look-alike names — is never guessed into an official set.

    **Validates: Requirements 15.4**
    """
    module_name, repo_url = source
    assert classify_plugin_set(module_name, repo_url) == CLASSIFICATION_UNCLASSIFIED


@settings(max_examples=25)
@given(source=st.one_of(
    official_sources().map(lambda s: (s[0], s[1])),
    arbitrary_sources(),
))
def test_every_classification_has_explanation(source):
    """**Feature: custom-node-designer, Property 6: Plugin-set classification is exact**

    For all sources, the classification is one of the four taxonomy
    values and carries a non-empty plain-language explanation.

    **Validates: Requirements 15.3**
    """
    module_name, repo_url = source
    result = classify_plugin_set(module_name, repo_url)
    assert result in CLASSIFICATIONS
    explanation = EXPLANATIONS[result]
    assert isinstance(explanation, str)
    assert explanation.strip()
