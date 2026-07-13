"""Test setup: make the harness package importable from the repo."""

import os
import sys

TEST_SANDBOX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TEST_SANDBOX_DIR not in sys.path:
    sys.path.insert(0, TEST_SANDBOX_DIR)
