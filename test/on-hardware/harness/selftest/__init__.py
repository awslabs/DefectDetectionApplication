"""Edge_Test_Harness selftests package.

The ``__init__.py`` makes pytest import these modules package-qualified
(``selftest.test_config`` rather than bare ``test_config``), so a repo-wide
pytest run cannot collide with same-named test modules elsewhere in the tree
(e.g. ``test/backend-test/local_auth/test_config.py``).
"""
