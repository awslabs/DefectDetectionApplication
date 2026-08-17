"""Cost_Cap gate for the benchmark harness (protocol §6).

Pure function — no I/O, no AWS. Unit-tested in tests/test_cost_cap.py
(Property 3: Cost_Cap invariant; Requirements 2.4, 2.9).
"""


def should_provision(spend_so_far: float, projected_run_cost: float, cap: float) -> bool:
    """Return True only if launching a run with `projected_run_cost` keeps
    total spend strictly below the Cost_Cap.

    False whenever spend_so_far + projected_run_cost >= cap (reaching the cap
    exactly counts as reaching it — provisioning must stop, Req 2.9).
    Negative inputs are invalid: a negative spend or projection indicates a
    ledger bug, and the gate fails closed.
    """
    if spend_so_far < 0 or projected_run_cost < 0 or cap < 0:
        return False
    return spend_so_far + projected_run_cost < cap
