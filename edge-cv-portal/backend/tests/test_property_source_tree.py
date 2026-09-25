"""
Property tests for the Source_Editor's pure helpers in plugin_records.py
(custom-node-source-lifecycle tasks 1.5, 1.6, 1.7).

Pure functions under test: normalize_source_path (Property 2),
resulting_tree (Property 3), and stale_architectures (Property 4). The
moto-backed stack is only needed to import the module.
"""
import posixpath

import pytest
from hypothesis import given, strategies as st


@pytest.fixture(scope="module")
def module(aws_stack):
    return aws_stack.plugin_records


# --------------------------------------------------------------- strategies

_segment = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/\\\x00"),
    min_size=0, max_size=8,
)
_special = st.sampled_from(["..", ".", "", " ", "src", "plugin", "builds", "x86_64"])
_path_like = st.lists(st.one_of(_segment, _special), min_size=0, max_size=5).map("/".join)
_path_strings = st.one_of(
    _path_like,
    _path_like.map(lambda p: "/" + p),
    _path_like.map(lambda p: p + "/"),
    st.text(max_size=20),
)


def reference_valid(path):
    """The Property 2 oracle: the normalized trimmed path is non-empty, not
    '.', does not start with '/' or '..', contains no '..' segment, no
    segment with leading/trailing whitespace (normalization must be a fixed
    point), and no control character."""
    if not isinstance(path, str) or not path.strip():
        return False
    stripped = path.strip()
    if stripped.startswith("/") or stripped.startswith("\\"):
        return False
    norm = posixpath.normpath(stripped)
    if norm in (".", "") or norm.startswith(".."):
        return False
    segments = norm.split("/")
    if any(seg == ".." for seg in segments):
        return False
    if any(seg != seg.strip() for seg in segments):
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in norm)


# ---------------------------------------------------------------- Property 2

class TestSourcePathValidity:
    """**Feature: custom-node-source-lifecycle, Property 2: Source path
    validity predicate** — Validates: Requirements 1.3, 3.1"""

    @given(path=_path_strings)
    def test_accepts_iff_oracle_accepts(self, module, path):
        result = module.normalize_source_path(path)
        assert (result is not None) == reference_valid(path)
        if result is not None:
            # The normalized path is itself stable and confined.
            assert module.normalize_source_path(result) == result
            assert not result.startswith("/") and ".." not in result.split("/")

    @given(value=st.one_of(st.none(), st.integers(), st.lists(st.text())))
    def test_non_strings_rejected(self, module, value):
        assert module.normalize_source_path(value) is None


# ---------------------------------------------------------------- Property 3

_rel_paths = st.lists(
    st.from_regex(r"[a-z]{1,4}(/[a-z0-9]{1,4}){0,2}\.[a-z]{1,3}", fullmatch=True),
    max_size=8, unique=True)


class TestResultingTree:
    """**Feature: custom-node-source-lifecycle, Property 3: Resulting-tree
    computation** — Validates: Requirements 1.4, 1.7"""

    @given(listing=_rel_paths, submitted=_rel_paths, delete=_rel_paths,
           mode=st.sampled_from(["merge", "replace"]))
    def test_matches_set_algebra(self, module, listing, submitted, delete, mode):
        files = {p: "content" for p in submitted}
        result = module.resulting_tree(listing, files, delete, mode)
        if mode == "replace":
            expected = set(files) - set(delete)
        else:
            expected = (set(listing) | set(files)) - set(delete)
        assert result == sorted(expected)
        # Deleted paths never survive; submitted-and-not-deleted always do.
        assert not (set(result) & set(delete))
        assert set(files) - set(delete) <= set(result)


# ---------------------------------------------------------------- Property 4

_arches = st.sampled_from(
    ["x86_64", "x86_64_nvidia", "arm64_cpu", "arm64_jp5", "arm64_jp6", "arm64_jp7"])
_entry = st.fixed_dictionaries({
    "buildStatus": st.sampled_from(["queued", "building", "succeeded", "failed"]),
}, optional={
    "sourceRevision": st.one_of(st.integers(min_value=1, max_value=6),
                                st.just("3"), st.none()),
})


class TestStaleness:
    """**Feature: custom-node-source-lifecycle, Property 4: Staleness is
    exactly a revision comparison** — Validates: Requirements 1.8, 1.9, 10.1"""

    @given(item_revision=st.one_of(st.none(), st.integers(min_value=1, max_value=6)),
           artifacts=st.dictionaries(_arches, _entry, max_size=6))
    def test_stale_iff_entry_revision_below_item_revision(self, module, item_revision,
                                                          artifacts):
        item = {"artifacts": artifacts}
        if item_revision is not None:
            item["source_revision"] = item_revision
        current = item_revision if item_revision is not None else 1

        expected = sorted(
            arch for arch, entry in artifacts.items()
            if int(entry.get("sourceRevision") or 1) < current
        )
        assert module.stale_architectures(item) == expected

    @given(artifacts=st.dictionaries(_arches, st.fixed_dictionaries(
        {"buildStatus": st.sampled_from(["succeeded", "failed"])}), max_size=6))
    def test_legacy_items_and_entries_are_never_stale(self, module, artifacts):
        assert module.stale_architectures({"artifacts": artifacts}) == []
        assert module.stale_architectures({"artifacts": artifacts,
                                           "source_revision": 1}) == []
