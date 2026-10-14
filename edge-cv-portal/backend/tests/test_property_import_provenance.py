"""Property test for import provenance (task 4.3).

**Feature: custom-node-designer, Property 7: Import provenance records the classification**

For all import sources (Module_Listing selections and arbitrary
repository URLs), the created Plugin_Record's provenance contains
repository URL, revision, importing user, retrieval timestamp, and a
classification equal to ``classify_plugin_set`` applied to that source.

**Validates: Requirements 4.2, 15.5**

The function under test (`plugin_importer.import_provenance`) is pure,
so the property is exercised directly with no AWS involvement. The
module is imported through the shared moto-backed session fixture only
so the real `shared_utils` layer (not a test fake) backs the import.

The source generators restate the official plugin-set ground truth from
the requirements (15.1) - mirroring the workflow_core classification
property test (task 1.5) - so the test cannot silently agree with a
wrong classification table.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog.classification import (
    CLASSIFICATION_BAD,
    CLASSIFICATION_GOOD,
    CLASSIFICATION_UGLY,
    CLASSIFICATION_UNCLASSIFIED,
    classify_plugin_set,
)


@pytest.fixture(scope="session")
def importer(aws_stack):
    """The real plugin_importer module, imported via the session stack."""
    return aws_stack.plugin_importer


# ---------------------------------------------------------------------------
# Ground truth: the official plugin-set module names and their expected
# classifications, restated here from the requirements (15.1).
# ---------------------------------------------------------------------------

OFFICIAL_MODULES = {
    "gst-plugins-good": CLASSIFICATION_GOOD,
    "gst-plugins-bad": CLASSIFICATION_BAD,
    "gst-plugins-ugly": CLASSIFICATION_UGLY,
}

official_module_names = st.sampled_from(sorted(OFFICIAL_MODULES))

_segment_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_."


def _strip_git(segment):
    return segment[: -len(".git")] if segment.endswith(".git") else segment


#: URL path segments guaranteed not to be an official plugin-set name,
#: even after the classifier strips a ``.git`` suffix.
_safe_segment = st.text(
    alphabet=_segment_alphabet, min_size=1, max_size=25
).filter(lambda s: _strip_git(s) not in OFFICIAL_MODULES and s != ".git")


# --------------------------------------------- official import sources

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
        host = draw(st.sampled_from(
            ["cgit.freedesktop.org", "anongit.freedesktop.org"]))
        suffix = draw(st.sampled_from(["", "/"]))
        url = "%s://%s/gstreamer/%s%s" % (scheme, host, module, suffix)
    else:  # release
        url = "https://gstreamer.freedesktop.org/src/%s/" % module
    return url, expected


@st.composite
def official_sources(draw):
    """(module_name, repo_url, expected) where the source is official
    either by Module_Listing selection (module name) or by known
    repository location."""
    if draw(st.booleans()):
        # Module_Listing selection: the official module name identifies
        # the set even when the accompanying URL is a non-official one.
        module = draw(official_module_names)
        url = draw(st.one_of(arbitrary_urls(), official_urls().map(lambda u: u[0])))
        return module, url, OFFICIAL_MODULES[module]
    # Arbitrary-URL import of a known official repository location; the
    # module name, if any, is a non-official one.
    url, expected = draw(official_urls())
    name = draw(st.one_of(st.none(), st.just(""), arbitrary_module_names()))
    return name, url, expected


# -------------------------------------------- arbitrary import sources

def arbitrary_module_names():
    """Module names that are not an official plugin-set name."""
    return st.text(max_size=30).filter(
        lambda s: s.strip() not in OFFICIAL_MODULES
    )


@st.composite
def _other_host_urls(draw):
    """Public repositories on non-freedesktop hosts, including ones
    deliberately named after official plugin sets (15.4)."""
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
    shape = draw(st.sampled_from(["gitlab-other-ns", "gitlab-safe", "release"]))
    if shape == "gitlab-other-ns":
        first = draw(_safe_segment.filter(lambda s: _strip_git(s) != "gstreamer"))
        rest = draw(st.lists(
            st.one_of(_safe_segment, official_module_names), max_size=3
        ))
        return "https://gitlab.freedesktop.org/%s" % "/".join([first] + rest)
    if shape == "gitlab-safe":
        rest = draw(st.lists(_safe_segment, max_size=3))
        return "https://gitlab.freedesktop.org/%s" % "/".join(["gstreamer"] + rest)
    segments = draw(st.lists(_safe_segment, min_size=1, max_size=3))
    return "https://gstreamer.freedesktop.org/%s" % "/".join(segments)


#: URL-shaped junk on random hosts; excluding "freedesktop.org" keeps
#: them non-official by construction.
_junk_urls = st.text(min_size=1, max_size=40).filter(
    lambda s: "freedesktop.org" not in s
).map(lambda s: "https://example.org/" + s)


def arbitrary_urls():
    """Repository URLs that are not a known official location."""
    return st.one_of(
        _other_host_urls(),
        _freedesktop_non_official_urls(),
        _junk_urls,
    )


@st.composite
def arbitrary_sources(draw):
    """(module_name, repo_url) with no official identity at all."""
    name = draw(st.one_of(st.none(), st.just(""), arbitrary_module_names()))
    url = draw(arbitrary_urls())
    return name, url


# ----------------------------------------------------- other inputs

#: Any import source: official or arbitrary, expected value dropped.
any_sources = st.one_of(
    official_sources().map(lambda s: (s[0], s[1])),
    arbitrary_sources(),
)

#: User-supplied revisions: omitted (None / empty string, meaning the
#: default branch was cloned) or a git-ish branch / tag / SHA string.
revisions = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet="abcdef0123456789", min_size=7, max_size=40),
    st.text(alphabet=_segment_alphabet + "/", min_size=1, max_size=30),
)

user_ids = st.text(min_size=1, max_size=40)

timestamps = st.integers(min_value=0, max_value=2**53)


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

@settings(max_examples=25)
@given(source=any_sources, revision=revisions, user_id=user_ids,
       timestamp=timestamps)
def test_provenance_carries_all_fields_and_the_classification(
        importer, source, revision, user_id, timestamp):
    """**Feature: custom-node-designer, Property 7: Import provenance records the classification**

    For all import sources, the provenance contains the repository URL,
    the retrieved revision (DEFAULT_REVISION when the default branch
    was used), the importing user, the retrieval timestamp, and a
    classification equal to `classify_plugin_set` applied to that
    source.

    **Validates: Requirements 4.2, 15.5**
    """
    module_name, repo_url = source
    provenance = importer.import_provenance(
        repo_url, revision, module_name, user_id, timestamp)

    assert provenance["repoUrl"] == repo_url
    assert provenance["revision"] == (revision or importer.DEFAULT_REVISION)
    assert provenance["importedBy"] == user_id
    assert provenance["importedAt"] == timestamp
    assert provenance["classification"] == classify_plugin_set(
        module_name, repo_url)


@settings(max_examples=25)
@given(source=official_sources(), revision=revisions, user_id=user_ids,
       timestamp=timestamps)
def test_official_sources_record_their_plugin_set(
        importer, source, revision, user_id, timestamp):
    """**Feature: custom-node-designer, Property 7: Import provenance records the classification**

    Module_Listing selections and known official repository locations
    record exactly their official plugin-set classification.

    **Validates: Requirements 4.2, 15.5**
    """
    module_name, repo_url, expected = source
    provenance = importer.import_provenance(
        repo_url, revision, module_name, user_id, timestamp)
    assert provenance["classification"] == expected


@settings(max_examples=25)
@given(source=arbitrary_sources(), revision=revisions, user_id=user_ids,
       timestamp=timestamps)
def test_arbitrary_sources_record_unclassified(
        importer, source, revision, user_id, timestamp):
    """**Feature: custom-node-designer, Property 7: Import provenance records the classification**

    Arbitrary public repository URLs with no official identity record
    the unclassified classification.

    **Validates: Requirements 4.2, 15.5**
    """
    module_name, repo_url = source
    provenance = importer.import_provenance(
        repo_url, revision, module_name, user_id, timestamp)
    assert provenance["classification"] == CLASSIFICATION_UNCLASSIFIED
