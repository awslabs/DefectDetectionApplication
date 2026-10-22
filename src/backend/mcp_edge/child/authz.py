# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The edge role gate (design §5 ``child/authz.py``, D13; Property 10;
Requirements 10.2-10.6, 16.2).

``decide(spec, principal, mutating_enabled, args)`` is pure:

* a read-only tool is allowed for every principal, ``anonymous`` included
  (the Backend_API already admitted the request);
* a mutating tool requires the mutating switch (else
  ``mutating_tools_disabled``), a Local_Session_Token (``source ==
  'local_session'``) and a role in ``{Operator, UseCaseAdmin, PortalAdmin}``
  (else ``not_authorized``);
* a destructive tool additionally requires ``confirm`` equal to the named
  argument, the target of the ``oneOf`` branch present, or the spec's
  ``confirm_literal`` (else ``confirmation_required``).
"""
import re
from dataclasses import dataclass
from typing import Optional, Union

MUTATING_ROLES = frozenset({'Operator', 'UseCaseAdmin', 'PortalAdmin'})
MUTATING_SOURCE = 'local_session'
_INDEXED_RE = re.compile(r'^([a-z_][a-z0-9_]*)\[(\d+)\]$')


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class Denied:
    category: str
    message: str


ALLOWED = Allowed()


def _target_value(args: dict, target: str):
    match = _INDEXED_RE.match(target)
    if match:
        value = args.get(match.group(1))
        index = int(match.group(2))
        return value[index] if isinstance(value, list) and len(value) > index else None
    return args.get(target)


def expected_confirm(spec, args: dict) -> Optional[str]:
    """The value ``confirm`` must equal, or None when none can be named."""
    target = spec.confirm_target
    if isinstance(target, tuple):
        for branch, branch_target in zip(spec.input_schema.get('oneOf', ()), target):
            name = _INDEXED_RE.sub(r'\1', branch_target)
            if all(r in args for r in branch.get('required', ())) and name in args:
                return _target_value(args, branch_target)
        return None
    if target:
        return _target_value(args, target)
    return spec.confirm_literal


def decide(spec, principal, mutating_enabled: bool,
           args: Optional[dict] = None) -> Union[Allowed, Denied]:
    if spec.read_only:
        return ALLOWED
    if not mutating_enabled:
        return Denied('mutating_tools_disabled',
                      f'{spec.name} is mutating and mutating tools are disabled on this device')
    if principal.source != MUTATING_SOURCE or principal.role not in MUTATING_ROLES:
        # Data Models "Tool error result (both servers)"
        return Denied('not_authorized', f'Operator role or higher required to invoke {spec.name}')
    if spec.destructive:
        args = args or {}
        expected = expected_confirm(spec, args)
        confirm = args.get('confirm')
        if expected is None or confirm is None or str(confirm) != str(expected):
            what = repr(spec.confirm_literal) if spec.confirm_literal else spec.confirm_target
            return Denied('confirmation_required',
                          f'{spec.name} is destructive; pass confirm={what}')
    return ALLOWED
