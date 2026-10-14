"""Smoke tests for the workflow_core package skeleton and test setup.

Verifies the package layout imports cleanly and that the hypothesis
profile registered in conftest.py (25+ examples per property for fast
local runs) is active for this test session.
"""

from hypothesis import given, settings
from hypothesis import strategies as st


def test_package_imports():
    import workflow_core
    import workflow_core.catalog
    import workflow_core.compiler
    import workflow_core.serializer
    import workflow_core.validator

    assert workflow_core.__version__


def test_hypothesis_profile_runs_at_least_25_examples():
    assert settings().max_examples >= 25


def test_hypothesis_executes_properties():
    """Sanity-check that hypothesis is wired up and generates examples."""
    executed = []

    @given(st.integers())
    def prop(n):
        executed.append(n)

    prop()
    assert len(executed) >= 25
