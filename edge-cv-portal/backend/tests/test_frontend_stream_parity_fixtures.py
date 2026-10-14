"""
The Workflow_Builder's parity fixtures are current
(rtsp-rtmp-stream-cameras task 11.2).

``edge-cv-portal/frontend/src/pages/workflows/__fixtures__/`` holds two
corpora generated from the Python rules (``frontend_parity_fixtures.py``):
the Stream_URL verdicts the TypeScript ``streamUrl.ts`` port must reproduce
(Property 2) and the validator findings the ``inlineChecks.ts`` mirrors
must reproduce (Property 6). The frontend tests replay them; this test
fails whenever the committed files differ from what the Python rules
generate now, so a Python rule change cannot leave the ports silently
behind. Regenerate with::

    ~/.venvs/dda-portal-tests/bin/python tests/frontend_parity_fixtures.py --write

Requirements: 2.1, 2.2, 2.6
"""
import os

import pytest

import frontend_parity_fixtures as fixtures


@pytest.mark.parametrize("path", sorted(fixtures.generated_files()))
def test_committed_fixture_matches_the_python_rules(path):
    expected = fixtures.generated_files()[path]
    assert os.path.exists(path), (
        f"{path} is missing; run tests/frontend_parity_fixtures.py --write")
    with open(path, encoding="utf-8") as handle:
        committed = handle.read()
    assert committed == expected, (
        f"{os.path.basename(path)} is stale: the Python rules changed. Run "
        "tests/frontend_parity_fixtures.py --write and then the frontend "
        "parity tests, and fix the TypeScript port if they fail.")


def test_the_corpora_exercise_every_mirrored_rule():
    """Guards the generator: every code the frontend mirrors occurs, and
    the Stream_URL corpus has accepted values and every problem code."""
    inline = fixtures.inline_parity_corpus()
    codes = {finding[0] for entry in inline["entries"]
             for finding in entry["findings"]}
    assert codes == set(fixtures.MIRRORED_CODES)
    assert any(not entry["findings"] for entry in inline["entries"])

    stream = fixtures.stream_url_corpus()
    stream_codes = {entry["code"] for entry in stream["entries"]}
    assert stream_codes == {None, "invalid_url", "scheme_not_allowed",
                            "no_host", "user_info", "secret_query_parameter"}
