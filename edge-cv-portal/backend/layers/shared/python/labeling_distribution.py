"""
Labeling Distribution Utility
Pure functions for distributing labeling Task_Assignments across
Labeling_Team members using a deterministic round-robin.

Used by the DDA Labeling System worker for:
- Initial distribution of a job's tasks across team members (Req 5.1, 5.2)
- Reassigning a removed member's unsubmitted tasks (Req 5.3)
- Assigning unassigned tasks when a member joins a blocked job (Req 5.5)

Determinism: members are sorted before assignment, so the same inputs
always produce the same assignment. Round-robin guarantees per-member
counts differ by at most one.
"""
from typing import Dict, List


def distribute(task_ids: List[str], member_ids: List[str]) -> Dict[str, str]:
    """
    Assign each task to exactly one member using deterministic round-robin.

    Members are sorted for determinism; task i is assigned to
    member[i % len(members)]. Per-member counts differ by at most one.

    Args:
        task_ids: Task identifiers to assign (order preserved)
        member_ids: Team member identifiers eligible for assignment

    Returns:
        Mapping of task_id -> member_id covering every task exactly once.
        Empty dict when there are no tasks or no members.
    """
    if not task_ids or not member_ids:
        return {}

    members = sorted(member_ids)
    n = len(members)
    return {task_id: members[i % n] for i, task_id in enumerate(task_ids)}


def rebalance(unassigned_task_ids: List[str], member_ids: List[str]) -> Dict[str, str]:
    """
    Reassign only the given unassigned tasks across members.

    Same deterministic round-robin as distribute(), applied to the subset
    of tasks being (re)assigned: a removed member's unsubmitted tasks, or
    a blocked job's unassigned tasks when a member is added. Reassigned
    counts per member differ by at most one.

    Args:
        unassigned_task_ids: Task identifiers being (re)assigned
        member_ids: Current team member identifiers

    Returns:
        Mapping of task_id -> member_id covering every given task exactly
        once. Empty dict when there are no tasks or no members.
    """
    return distribute(unassigned_task_ids, member_ids)
