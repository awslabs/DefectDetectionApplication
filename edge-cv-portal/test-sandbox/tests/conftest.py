"""Test setup: make the harness package importable from the repo."""

import os
import sys

TEST_SANDBOX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TEST_SANDBOX_DIR not in sys.path:
    sys.path.insert(0, TEST_SANDBOX_DIR)

# Hypothesis profile: cap property tests at 25 examples for fast local runs.
# Per-test @settings decorators take precedence; keep them at or below this
# budget. Override with HYPOTHESIS_PROFILE=ci for a larger run.
from hypothesis import settings as _hyp_settings  # noqa: E402

_hyp_settings.register_profile("sandbox-fast", max_examples=25)
_hyp_settings.register_profile("ci", max_examples=100)
_hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "sandbox-fast"))
