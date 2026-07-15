"""Unit tests for workflow_core.catalog.classification.

Covers each official plugin set by module name and by known repository
URL forms, arbitrary URLs classifying as unclassified, and the fixed
plain-language explanations (Requirements 15.3, 15.4).
"""

import pytest

from workflow_core.catalog.classification import (
    CLASSIFICATION_BAD,
    CLASSIFICATION_GOOD,
    CLASSIFICATION_UGLY,
    CLASSIFICATION_UNCLASSIFIED,
    CLASSIFICATIONS,
    EXPLANATIONS,
    classify_plugin_set,
)


class TestClassifyByModuleName:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("gst-plugins-good", CLASSIFICATION_GOOD),
            ("gst-plugins-bad", CLASSIFICATION_BAD),
            ("gst-plugins-ugly", CLASSIFICATION_UGLY),
        ],
    )
    def test_official_set_names(self, name, expected):
        assert classify_plugin_set(name, None) == expected

    def test_name_wins_without_url(self):
        assert classify_plugin_set("gst-plugins-good", "") == CLASSIFICATION_GOOD

    def test_surrounding_whitespace_is_ignored(self):
        assert classify_plugin_set("  gst-plugins-bad ", None) == CLASSIFICATION_BAD

    @pytest.mark.parametrize(
        "name",
        [
            "gst-plugins-base",
            "gstreamer",
            "gst-plugins-goodish",
            "my-gst-plugins-good",
            "gst-libav",
            "some-random-plugin",
            "",
        ],
    )
    def test_other_names_are_unclassified(self, name):
        assert classify_plugin_set(name, None) == CLASSIFICATION_UNCLASSIFIED

    def test_none_inputs_are_unclassified(self):
        assert classify_plugin_set(None, None) == CLASSIFICATION_UNCLASSIFIED


class TestClassifyByRepoUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            # Per-set GitLab repos (with and without .git / trailing slash)
            (
                "https://gitlab.freedesktop.org/gstreamer/gst-plugins-good",
                CLASSIFICATION_GOOD,
            ),
            (
                "https://gitlab.freedesktop.org/gstreamer/gst-plugins-bad.git",
                CLASSIFICATION_BAD,
            ),
            (
                "https://gitlab.freedesktop.org/gstreamer/gst-plugins-ugly/",
                CLASSIFICATION_UGLY,
            ),
            # Monorepo subproject paths
            (
                "https://gitlab.freedesktop.org/gstreamer/gstreamer/-/tree/"
                "main/subprojects/gst-plugins-good",
                CLASSIFICATION_GOOD,
            ),
            (
                "https://gitlab.freedesktop.org/gstreamer/gstreamer/-/tree/"
                "main/subprojects/gst-plugins-bad",
                CLASSIFICATION_BAD,
            ),
            (
                "https://gitlab.freedesktop.org/gstreamer/gstreamer/-/tree/"
                "main/subprojects/gst-plugins-ugly",
                CLASSIFICATION_UGLY,
            ),
            # Historical cgit / anongit mirrors
            (
                "https://cgit.freedesktop.org/gstreamer/gst-plugins-good/",
                CLASSIFICATION_GOOD,
            ),
            (
                "git://anongit.freedesktop.org/gstreamer/gst-plugins-ugly",
                CLASSIFICATION_UGLY,
            ),
            # Source release tree
            (
                "https://gstreamer.freedesktop.org/src/gst-plugins-bad/",
                CLASSIFICATION_BAD,
            ),
        ],
    )
    def test_known_official_urls(self, url, expected):
        assert classify_plugin_set(None, url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            # Arbitrary public repositories are never guessed into a set,
            # even when named like one (Requirement 15.4).
            "https://github.com/someone/gst-plugins-good",
            "https://gitlab.com/gstreamer/gst-plugins-good",
            "https://example.com/gstreamer/gst-plugins-bad",
            # freedesktop hosts without an official set path
            "https://gitlab.freedesktop.org/gstreamer/gstreamer",
            "https://gitlab.freedesktop.org/someuser/gst-plugins-good",
            "https://gitlab.freedesktop.org/gstreamer/gst-plugins-base",
            "https://cgit.freedesktop.org/mesa/mesa",
            # Malformed / odd inputs
            "not a url",
            "ftp://gitlab.freedesktop.org/gstreamer/gst-plugins-good",
            "",
        ],
    )
    def test_everything_else_is_unclassified(self, url):
        assert classify_plugin_set(None, url) == CLASSIFICATION_UNCLASSIFIED

    def test_module_selected_from_listing_matches_by_name_and_url(self):
        # A Module_Listing entry carries both the name and the repo URL.
        assert (
            classify_plugin_set(
                "gst-plugins-ugly",
                "https://gitlab.freedesktop.org/gstreamer/gst-plugins-ugly.git",
            )
            == CLASSIFICATION_UGLY
        )


class TestExplanations:
    def test_every_classification_has_a_non_empty_explanation(self):
        assert set(EXPLANATIONS) == set(CLASSIFICATIONS)
        for value in CLASSIFICATIONS:
            assert isinstance(EXPLANATIONS[value], str)
            assert EXPLANATIONS[value].strip()

    def test_explanation_texts_match_requirement_15_3(self):
        assert EXPLANATIONS[CLASSIFICATION_GOOD] == (
            "good indicates a well-maintained, well-tested, properly "
            "licensed plugin set"
        )
        assert EXPLANATIONS[CLASSIFICATION_BAD] == (
            "bad indicates a plugin set lacking upstream review, testing, "
            "or active maintenance"
        )
        assert EXPLANATIONS[CLASSIFICATION_UGLY] == (
            "ugly indicates a plugin set of good quality that carries "
            "licensing or distribution concerns"
        )
        assert EXPLANATIONS[CLASSIFICATION_UNCLASSIFIED] == (
            "unclassified indicates a plugin outside the official GStreamer "
            "plugin sets that warrants the highest caution"
        )
