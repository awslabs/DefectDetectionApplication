"""The one topology predicate (workflow-telemetry-dashboards design §2.3).

The trunk carries two disagreeing predicates: ``assume_usecase_role`` returns
the function's own credentials only for a ``:root`` role ARN, while
``is_cross_account_setup`` calls any ``:role/`` ARN multi-account. A use case
in the Portal account can carry a ``:role/DDAPortalAccessRole`` ARN, so the
telemetry and debug code decide on the account id alone: ``portal`` when the
use case's devices are in the Portal account, else ``cross``.
"""
import os
from typing import Dict

__all__ = ["PORTAL", "CROSS", "telemetry_topology"]

PORTAL = "portal"
CROSS = "cross"


def telemetry_topology(usecase: Dict) -> str:
    """``'portal'`` when the use case's ``account_id`` equals the function's
    ``PORTAL_ACCOUNT_ID`` (a required environment value: every function of
    the telemetry and debug stacks carries it, and a missing value is a
    misconfigured deploy, not a cross-account use case), else ``'cross'``."""
    portal_account_id = os.environ["PORTAL_ACCOUNT_ID"]
    return PORTAL if str(usecase.get("account_id") or "") == portal_account_id else CROSS

