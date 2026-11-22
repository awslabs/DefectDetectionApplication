"""Test fixtures for the portal backend suite (importable as ``fixtures.*``).
Only *builders* live here — every binary fixture is generated into ``tmp_path``
at test time, nothing binary is checked in, except two small JPEGs whose exact
pixel geometry is the point of the test (``inspection_replay_parity.input.jpg``
and ``synthetic_source_4608x3288.jpg``).
"""
