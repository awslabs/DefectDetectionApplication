"""Boundary tests for the Cost_Cap gate.

**Property 3: Cost_Cap invariant** — should_provision(spend, projected, cap)
returns false whenever spend + projected >= cap.
**Validates: Requirements 2.4, 2.9**
"""

from cost_cap import should_provision

CAP = 500.0


def test_below_cap_allows_provisioning():
    # 300 spent + 100 projected = 400 < 500
    assert should_provision(300.0, 100.0, CAP) is True


def test_exactly_at_cap_blocks_provisioning():
    # 400 spent + 100 projected = 500 == cap → reaching the cap stops provisioning
    assert should_provision(400.0, 100.0, CAP) is False


def test_above_cap_blocks_provisioning():
    # 450 spent + 100 projected = 550 > 500
    assert should_provision(450.0, 100.0, CAP) is False


def test_zero_spend_allows_first_run():
    assert should_provision(0.0, 50.0, CAP) is True


def test_projected_cost_alone_exceeding_cap_blocks():
    # No spend yet, but a single run projected over the cap must be blocked
    assert should_provision(0.0, 600.0, CAP) is False
