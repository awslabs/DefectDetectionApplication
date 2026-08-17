import sys
from pathlib import Path

# Make the harness modules importable without packaging (plain scripts by design).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
