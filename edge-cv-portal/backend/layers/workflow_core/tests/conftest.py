"""Shared pytest/hypothesis configuration for workflow_core tests.

Registers and loads a hypothesis profile that caps property tests at
25 examples for fast local runs (HYPOTHESIS_PROFILE=ci for larger runs).
"""

import os
import sys

from hypothesis import HealthCheck, settings

# Make the layer's package importable without installation
# (mirrors how the Lambda layer exposes it on sys.path under python/).
# Appended rather than prepended: python/ also carries the layer's
# vendored Lambda-runtime dependencies (CPython 3.11 manylinux wheels,
# e.g. jsonschema's rpds), which must not shadow the host interpreter's
# own packages when the tests run locally.
_PACKAGE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python")
if _PACKAGE_ROOT not in sys.path:
    sys.path.append(_PACKAGE_ROOT)

# Default example budget for property tests. Reduced from the original
# 100-example profile to keep local suite runtime low; use the "ci"
# profile (HYPOTHESIS_PROFILE=ci) for a more exhaustive run.
settings.register_profile(
    "workflow-manager",
    max_examples=25,
    suppress_health_check=[HealthCheck.too_slow],
)

# Allow overriding via HYPOTHESIS_PROFILE (e.g. a larger "ci" run),
# defaulting to the fast 25-example profile.
settings.register_profile("ci", max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "workflow-manager"))
