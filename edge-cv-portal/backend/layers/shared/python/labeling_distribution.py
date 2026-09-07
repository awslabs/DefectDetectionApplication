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

Also the home of the Podium_Ranking contract
(labeling-job-cleanup-work-stealing-and-podium, Req 7.1-7.4, 7.6):
podium_ranking() ranks a job's submitters by descending Submitted
count, ties broken by earliest final submission timestamp, then by
ascending user id, and emits at most three podium entries. One shared
pure function beside distribute()/rebalance(), consumed by both the
admin job detail payload and the labeler pool route.
"""
from typing import Any, Dict, Iterable, List, Tuple


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


def podium_ranking(submissions: Iterable[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    """
    Podium_Ranking (labeling-job-cleanup-work-stealing-and-podium
    Req 7.1-7.4): rank submitters by (-submitted_count,
    final_submission_ts, user_id) and emit at most three entries
    [{'place': 1..3, 'user_id', 'submitted', 'final_submitted_at'}].

    `submissions` is an iterable of (user_id, submitted_at) pairs —
    one per Submitted task. Pure and total: junk-free by construction
    (callers pass what they queried), deterministic under input
    permutation, empty in → empty out.

    Args:
        submissions: (user_id, submitted_at) pairs, one per Submitted task

    Returns:
        At most three dicts {'place', 'user_id', 'submitted',
        'final_submitted_at'} in rank order; fewer when fewer submitters
        exist, an empty list when there are none.
    """
    counts: Dict[str, int] = {}
    finals: Dict[str, Any] = {}
    for user_id, submitted_at in submissions:
        counts[user_id] = counts.get(user_id, 0) + 1
        if user_id not in finals or submitted_at > finals[user_id]:
            finals[user_id] = submitted_at

    ranked = sorted(counts, key=lambda user_id: (-counts[user_id], finals[user_id], user_id))
    return [
        {
            'place': i + 1,
            'user_id': user_id,
            'submitted': counts[user_id],
            'final_submitted_at': finals[user_id],
        }
        for i, user_id in enumerate(ranked[:3])
    ]
