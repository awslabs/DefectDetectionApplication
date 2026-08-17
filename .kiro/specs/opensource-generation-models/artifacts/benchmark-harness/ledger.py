"""Cost-ledger helpers for the benchmark harness (protocol §6).

The ledger is the markdown cost table in
`artifacts/benchmark-results/README.md`. Rows are appended at instance launch
(status `launched`, projected cost) and updated at terminate (status
`complete` / `incomplete`, actual cost). `spend_so_far` counts actual cost for
finished rows and projected cost for in-flight rows, so the Cost_Cap gate is
conservative.

Table format (header must match exactly):

| run_id | model | instance_type | status | launch_utc | terminate_utc | projected_cost_usd | actual_cost_usd |
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

LEDGER_HEADER = (
    "| run_id | model | instance_type | status | launch_utc | terminate_utc "
    "| projected_cost_usd | actual_cost_usd |"
)
LEDGER_COLUMNS = [
    "run_id", "model", "instance_type", "status", "launch_utc",
    "terminate_utc", "projected_cost_usd", "actual_cost_usd",
]
_FINISHED_STATUSES = {"complete", "incomplete"}


def _parse_row(line: str) -> Optional[Dict[str, str]]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) != len(LEDGER_COLUMNS):
        return None
    return dict(zip(LEDGER_COLUMNS, cells))


def read_ledger(readme_path: Path) -> List[Dict[str, str]]:
    """Parse ledger rows out of the README's cost table."""
    text = Path(readme_path).read_text()
    lines = text.splitlines()
    rows: List[Dict[str, str]] = []
    in_table = False
    for line in lines:
        if line.strip() == LEDGER_HEADER:
            in_table = True
            continue
        if in_table:
            if re.match(r"^\|[\s:-]+\|", line):  # separator row
                continue
            if not line.strip().startswith("|"):
                break  # table ended
            row = _parse_row(line)
            if row and row["run_id"] not in ("run_id", ""):
                rows.append(row)
    return rows


def _cost(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def spend_so_far(rows: List[Dict[str, str]]) -> float:
    """Actual cost of finished runs + projected cost of in-flight runs."""
    total = 0.0
    for row in rows:
        if row["status"] in _FINISHED_STATUSES:
            total += _cost(row["actual_cost_usd"])
        else:  # launched / in-flight: count the projection conservatively
            total += _cost(row["projected_cost_usd"])
    return total


def _format_row(row: Dict[str, str]) -> str:
    return "| " + " | ".join(str(row.get(c, "")) for c in LEDGER_COLUMNS) + " |"


def append_row(readme_path: Path, row: Dict[str, str]) -> None:
    """Append a ledger row (used at instance launch)."""
    path = Path(readme_path)
    lines = path.read_text().splitlines()
    # insert after the last table line following the header
    try:
        header_idx = next(i for i, l in enumerate(lines) if l.strip() == LEDGER_HEADER)
    except StopIteration:
        raise ValueError(f"Ledger table header not found in {path}")
    insert_at = header_idx + 1
    while insert_at < len(lines) and lines[insert_at].strip().startswith("|"):
        insert_at += 1
    lines.insert(insert_at, _format_row(row))
    path.write_text("\n".join(lines) + "\n")


def update_row(readme_path: Path, run_id: str, updates: Dict[str, str]) -> None:
    """Update an existing ledger row by run_id (used at terminate)."""
    path = Path(readme_path)
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        row = _parse_row(line) if line.strip().startswith("|") else None
        if row and row.get("run_id") == run_id:
            row.update(updates)
            lines[i] = _format_row(row)
            path.write_text("\n".join(lines) + "\n")
            return
    raise ValueError(f"Ledger row not found for run_id={run_id}")
