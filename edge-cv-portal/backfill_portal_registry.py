#!/usr/bin/env python3
"""Backfill the Portal_Identity registry from the Cognito user pool.

Spec: `.kiro/specs/portal-jwt-role-privilege-escalation` (Requirement 2,
design.md Decision 5).

Why this exists
---------------
After this bugfix, portal privilege comes from a Portal_Identity row in
`dda-portal-user-roles` (keyed `user_id` = Cognito `sub`, `usecase_id`),
written only by the portal's own User Manager — a `custom:role` claim
grants nothing. The registry, however, was historically populated only by
Team Management's per-Use_Case grants, so almost no account has the global
row that enforcement requires. Turning `PORTAL_REGISTRY_ENFORCED` on
before this script has run would deny every user, including the bootstrap
`admin`.

This script therefore creates, for each **enabled** pool account, the
global registry row carrying the role that account effectively has today,
so enabling enforcement changes nobody's access (Requirement 2.3).

What it does
------------
* Paginates `cognito-idp list_users` over the portal pool.
* Skips **disabled** Cognito accounts, so they stay denied (Decision 5).
* For every enabled account, writes
  `{user_id: <sub>, usecase_id: 'global', role: <custom:role or Viewer>,
  username, email, status: 'enabled', assigned_by: 'backfill',
  assigned_at: <epoch seconds>}` — **only when the row is absent**, via a
  conditional write, so an existing row (a real portal assignment, or a
  previous run of this script) is never overwritten (Requirement 2.2).
* Prints a per-account plan and a summary, and is safely re-runnable.

It is **dry run by default**: nothing is written until `--apply` is
passed. It runs as an operator script rather than a deployment-time
custom resource so the plan can be reviewed before it is applied, and so
it is not coupled to a stack that could roll back (Decision 5).

Only `usecase_id='global'` rows are touched. Per-Use_Case grants belong to
Team Management and are left exactly as they are.

Usage
-----
    # review (writes nothing)
    ./backfill_portal_registry.py --user-pool-id us-east-2_XXXXXXXXX \
        --region us-east-2

    # apply, after reviewing the plan
    ./backfill_portal_registry.py --user-pool-id us-east-2_XXXXXXXXX \
        --region us-east-2 --apply

Requires credentials that can `cognito-idp:ListUsers` on the pool and
`dynamodb:GetItem`/`PutItem` on the registry table. Exit status is 0 when
the run completed, 1 when any account could not be processed, and 2 on a
usage error.
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

import boto3
from botocore.exceptions import ClientError

# --- Registry shape --------------------------------------------------------
#
# Kept deliberately literal instead of importing the Lambda shared layer:
# this script runs from an operator's shell with nothing but boto3 on the
# path, and `shared_utils` builds AWS clients at import time. The values
# below mirror `shared_utils`/`user_admin` and are pinned against
# `shared_utils.Role` by the spec's backfill property test.

DEFAULT_TABLE_NAME = 'dda-portal-user-roles'

# The account-level scope (shared_utils.GLOBAL_SCOPE). The global row is
# the provisioning record enforcement resolves privilege from.
GLOBAL_SCOPE = 'global'

# shared_utils.REGISTRY_STATUS_ENABLED.
STATUS_ENABLED = 'enabled'

# Roles the portal recognizes (shared_utils.Role values). A claim naming
# anything else resolves to Viewer today (the legacy step-4 fallthrough),
# so backfilling Viewer preserves that account's current access.
VALID_ROLES = (
    'Viewer',
    'Operator',
    'DataScientist',
    'UseCaseAdmin',
    'PortalAdmin',
    'DataLabeler',
)

# The role an account with no usable `custom:role` gets (Requirement 2.1).
DEFAULT_ROLE = 'Viewer'

# Recorded on every row this script writes, so an operator can tell a
# backfilled row from one the User Manager wrote.
ASSIGNED_BY = 'backfill'

# Cognito's `list_users` page size cap.
DEFAULT_PAGE_SIZE = 60

# --- Plan actions ----------------------------------------------------------

ACTION_CREATE = 'create'          # no row yet: this run writes one
ACTION_EXISTS = 'exists'          # a row is already there: left unchanged
ACTION_SKIP_DISABLED = 'skip-disabled'   # disabled account: stays denied
ACTION_SKIP_NO_SUB = 'skip-no-sub'       # no `sub`: no row can be addressed
ACTION_ERROR = 'error'            # this account could not be processed

# Actions that mean "nothing was or would be written".
NON_WRITING_ACTIONS = (ACTION_EXISTS, ACTION_SKIP_DISABLED,
                       ACTION_SKIP_NO_SUB, ACTION_ERROR)


@dataclass
class PlannedEntry:
    """What the backfill will do (or did) for one pool account."""

    username: str
    action: str
    user_id: Optional[str] = None
    email: str = ''
    claimed_role: Optional[str] = None
    role: Optional[str] = None
    enabled: bool = True
    detail: str = ''
    # The registry row written (apply) or that would be written (dry run).
    item: Optional[Dict[str, Any]] = None


@dataclass
class BackfillResult:
    """The outcome of one backfill run."""

    table_name: str
    user_pool_id: str
    applied: bool
    entries: List[PlannedEntry] = field(default_factory=list)

    def by_action(self, action: str) -> List[PlannedEntry]:
        return [e for e in self.entries if e.action == action]

    @property
    def scanned(self) -> int:
        return len(self.entries)

    @property
    def created(self) -> int:
        """Rows written (apply) or that would be written (dry run)."""
        return len(self.by_action(ACTION_CREATE))

    @property
    def unchanged(self) -> int:
        return len(self.by_action(ACTION_EXISTS))

    @property
    def skipped_disabled(self) -> int:
        return len(self.by_action(ACTION_SKIP_DISABLED))

    @property
    def skipped_no_sub(self) -> int:
        return len(self.by_action(ACTION_SKIP_NO_SUB))

    @property
    def errors(self) -> int:
        return len(self.by_action(ACTION_ERROR))


# --- Pure helpers ----------------------------------------------------------

def attributes_of(user: Dict[str, Any]) -> Dict[str, str]:
    """The `Attributes` list of a `list_users` record as a dict."""
    return {a.get('Name'): a.get('Value')
            for a in user.get('Attributes') or []
            if a.get('Name')}


def role_for(claimed_role: Optional[str]) -> str:
    """The role to backfill for a `custom:role` value (Requirement 2.1).

    The claim when it names a role the portal recognizes, otherwise
    `Viewer` — which is exactly what an absent or unrecognized claim
    resolves to today, so the account's access does not change when
    enforcement is enabled.

    The match is **exact**, deliberately: role resolution does
    `Role(claim)` (`shared_utils._legacy_role` step 4), so a padded value
    such as `' PortalAdmin'` is *not* a role today and its account's
    current effective role is `Viewer`. Normalizing whitespace here would
    backfill `PortalAdmin` for it and hand that account privilege it does
    not have — the opposite of "preserves current access" (Requirement
    2.1/2.3, caught by the backfill property test's role oracle).
    """
    if claimed_role and claimed_role in VALID_ROLES:
        return claimed_role
    return DEFAULT_ROLE


def plan_entry(user: Dict[str, Any]) -> PlannedEntry:
    """Classify one `list_users` record, before the registry is consulted.

    Returns an entry whose action is `skip-disabled` (the account is
    disabled in Cognito, so it must stay denied), `skip-no-sub` (nothing
    names the account's `sub`, so no row can be addressed — a row under an
    invented key would decide nothing while looking like provisioning), or
    `create` (a row should exist for this account).
    """
    attributes = attributes_of(user)
    username = user.get('Username') or ''
    claimed_role = attributes.get('custom:role')
    entry = PlannedEntry(
        username=username,
        action=ACTION_CREATE,
        user_id=attributes.get('sub'),
        email=attributes.get('email') or '',
        claimed_role=claimed_role,
        role=role_for(claimed_role),
        enabled=bool(user.get('Enabled', False)),
    )

    if not entry.enabled:
        entry.action = ACTION_SKIP_DISABLED
        entry.detail = 'Cognito account is disabled; it stays denied'
        return entry

    if not entry.user_id:
        entry.action = ACTION_SKIP_NO_SUB
        entry.detail = ('no sub attribute, so no registry row can be '
                        'addressed for this account')
        return entry

    return entry


def registry_item(entry: PlannedEntry,
                  now: Optional[int] = None) -> Dict[str, Any]:
    """The global Portal_Identity row for an account.

    Same shape the User Manager writes (`user_admin._put_registry_identity`)
    so a backfilled row is indistinguishable to the read path, apart from
    `assigned_by`.
    """
    return {
        'user_id': entry.user_id,
        'usecase_id': GLOBAL_SCOPE,
        'role': entry.role,
        'username': entry.username,
        'email': entry.email or '',
        'status': STATUS_ENABLED,
        'assigned_by': ASSIGNED_BY,
        'assigned_at': int(time.time() if now is None else now),
    }


# --- AWS access ------------------------------------------------------------

def iter_pool_users(cognito, user_pool_id: str,
                    page_size: int = DEFAULT_PAGE_SIZE
                    ) -> Iterator[Dict[str, Any]]:
    """Yield every user in the pool, paginating `list_users` fully."""
    params: Dict[str, Any] = {'UserPoolId': user_pool_id, 'Limit': page_size}
    while True:
        response = cognito.list_users(**params)
        for user in response.get('Users', []):
            yield user
        token = response.get('PaginationToken')
        if not token:
            return
        params['PaginationToken'] = token


def existing_identity(table, user_id: str) -> Optional[Dict[str, Any]]:
    """The account's global Portal_Identity row, or None."""
    return table.get_item(
        Key={'user_id': user_id, 'usecase_id': GLOBAL_SCOPE}
    ).get('Item')


def write_identity_if_absent(table, item: Dict[str, Any]) -> bool:
    """Conditionally write a global row; False when one already exists.

    The condition is what makes the script idempotent and non-destructive
    (Requirement 2.2): a second run, or a row the User Manager wrote in
    between, is left exactly as it is.
    """
    try:
        table.put_item(
            Item=item,
            ConditionExpression=('attribute_not_exists(user_id) AND '
                                 'attribute_not_exists(usecase_id)'),
        )
        return True
    except ClientError as error:
        if error.response.get('Error', {}).get('Code') == \
                'ConditionalCheckFailedException':
            return False
        raise


# --- Reporting -------------------------------------------------------------

def _role_note(entry: PlannedEntry) -> str:
    claim = entry.claimed_role if entry.claimed_role else '<none>'
    return f"claim={claim}"


def format_plan_line(entry: PlannedEntry) -> str:
    """One reviewable line per account."""
    parts = [f"  {entry.action:<13} {entry.username:<24}"]
    if entry.user_id:
        parts.append(f"sub={entry.user_id}")
    if entry.action in (ACTION_CREATE, ACTION_EXISTS):
        parts.append(f"role={entry.role}")
        parts.append(_role_note(entry))
    if entry.email:
        parts.append(f"email={entry.email}")
    if entry.detail:
        parts.append(f"- {entry.detail}")
    return ' '.join(parts)


def format_summary(result: BackfillResult) -> List[str]:
    """The end-of-run summary, one line per counter."""
    verb = 'created' if result.applied else 'to create'
    lines = [
        '',
        'SUMMARY',
        f"  pool accounts scanned : {result.scanned}",
        f"  global rows {verb:<9} : {result.created}",
        f"  already present       : {result.unchanged}",
        f"  skipped (disabled)    : {result.skipped_disabled}",
        f"  skipped (no sub)      : {result.skipped_no_sub}",
        f"  errors                : {result.errors}",
    ]
    if not result.applied:
        lines.append('')
        lines.append('  DRY RUN: nothing was written. Re-run with --apply '
                     'to write the rows above.')
    return lines


# --- The run --------------------------------------------------------------

def run_backfill(user_pool_id: str,
                 table_name: Optional[str] = None,
                 region: Optional[str] = None,
                 apply_changes: bool = False,
                 cognito=None,
                 dynamodb_resource=None,
                 page_size: int = DEFAULT_PAGE_SIZE,
                 out: Optional[Callable[[str], None]] = None
                 ) -> BackfillResult:
    """Plan (and optionally apply) the registry backfill for one pool.

    Args:
        user_pool_id: the portal Cognito user pool.
        table_name: registry table; defaults to `USER_ROLES_TABLE` in the
            environment, then `dda-portal-user-roles`.
        region: AWS region for the clients this function builds.
        apply_changes: False (the default) plans without writing.
        cognito / dynamodb_resource: injectable clients (tests).
        page_size: `list_users` page size.
        out: line sink; defaults to stdout.

    Returns:
        BackfillResult with one PlannedEntry per pool account.

    Raises:
        botocore.exceptions.ClientError: listing the pool failed. Failing
            loudly is deliberate — a partial listing would under-report
            the accounts that still need a row.
    """
    emit = out if out is not None else print
    table_name = table_name or default_table_name()
    client_kwargs = {'region_name': region} if region else {}
    cognito = cognito or boto3.client('cognito-idp', **client_kwargs)
    dynamodb_resource = dynamodb_resource or boto3.resource(
        'dynamodb', **client_kwargs)
    table = dynamodb_resource.Table(table_name)

    result = BackfillResult(table_name=table_name,
                            user_pool_id=user_pool_id,
                            applied=apply_changes)

    emit('Portal_Identity registry backfill')
    emit(f"  user pool : {user_pool_id}")
    emit(f"  registry  : {table_name}")
    emit(f"  region    : {region or '<default>'}")
    emit(f"  mode      : "
         f"{'APPLY (writes missing rows)' if apply_changes else 'DRY RUN'}")
    emit('')
    emit('PLAN')

    for user in iter_pool_users(cognito, user_pool_id, page_size=page_size):
        entry = plan_entry(user)

        if entry.action == ACTION_CREATE:
            try:
                current = existing_identity(table, entry.user_id)
            except ClientError as error:
                entry.action = ACTION_ERROR
                entry.detail = f"registry read failed: {error}"
                current = None
            else:
                if current is not None:
                    entry.action = ACTION_EXISTS
                    entry.item = current
                    current_role = current.get('role')
                    entry.role = current_role
                    entry.detail = 'existing registry row left unchanged'
                    if current_role != role_for(entry.claimed_role):
                        # Worth a reviewer's eye: the registry and the
                        # claim disagree. The registry wins, unchanged.
                        entry.detail += (
                            f" (differs from claim "
                            f"{entry.claimed_role or '<none>'})")

        if entry.action == ACTION_CREATE:
            entry.item = registry_item(entry)
            if apply_changes:
                try:
                    written = write_identity_if_absent(table, entry.item)
                except ClientError as error:
                    entry.action = ACTION_ERROR
                    entry.detail = f"registry write failed: {error}"
                else:
                    if not written:
                        # A row appeared between the read and the write
                        # (a concurrent portal assignment): the existing
                        # row stands, exactly as on a second run.
                        entry.action = ACTION_EXISTS
                        entry.detail = ('a registry row appeared during '
                                        'the run and was left unchanged')
                        entry.item = existing_identity(table, entry.user_id)

        result.entries.append(entry)
        emit(format_plan_line(entry))

    if not result.entries:
        emit('  (no accounts in the pool)')

    for line in format_summary(result):
        emit(line)

    return result


# --- CLI -----------------------------------------------------------------

def default_table_name() -> str:
    return os.environ.get('USER_ROLES_TABLE') or DEFAULT_TABLE_NAME


def default_user_pool_id() -> Optional[str]:
    return (os.environ.get('USER_POOL_ID')
            or os.environ.get('COGNITO_USER_POOL_ID'))


def default_region() -> Optional[str]:
    return os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=('Backfill the Portal_Identity registry '
                     '(dda-portal-user-roles) from the portal Cognito user '
                     'pool. Dry run unless --apply is given.'))
    parser.add_argument(
        '--user-pool-id', default=default_user_pool_id(),
        help=('the portal Cognito user pool id (default: $USER_POOL_ID / '
              '$COGNITO_USER_POOL_ID)'))
    parser.add_argument(
        '--table-name', default=default_table_name(),
        help=(f"registry table (default: $USER_ROLES_TABLE, else "
              f"{DEFAULT_TABLE_NAME})"))
    parser.add_argument(
        '--region', default=default_region(),
        help='AWS region (default: $AWS_REGION / $AWS_DEFAULT_REGION)')
    parser.add_argument(
        '--apply', action='store_true',
        help='write the missing rows (without this flag nothing is written)')
    parser.add_argument(
        '--page-size', type=int, default=DEFAULT_PAGE_SIZE,
        help=f"list_users page size (default: {DEFAULT_PAGE_SIZE})")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.user_pool_id:
        print('error: --user-pool-id is required (or set $USER_POOL_ID)',
              file=sys.stderr)
        return 2

    try:
        result = run_backfill(
            user_pool_id=args.user_pool_id,
            table_name=args.table_name,
            region=args.region,
            apply_changes=args.apply,
            page_size=args.page_size,
        )
    except ClientError as error:
        print(f"error: backfill aborted: {error}", file=sys.stderr)
        return 1

    return 1 if result.errors else 0


if __name__ == '__main__':
    sys.exit(main())
