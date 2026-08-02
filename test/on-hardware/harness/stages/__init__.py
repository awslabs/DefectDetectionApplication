"""Edge_Test_Harness stage modules package.

The ``__init__.py`` makes pytest import the stage modules package-qualified
(``stages.test_00_health`` rather than bare ``test_00_health``), so a
repo-wide pytest run cannot collide with same-named test modules elsewhere
in the tree.
"""
