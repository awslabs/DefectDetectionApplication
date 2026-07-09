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
"""Repo audit for the IAM / authorization bug-condition patterns
(security-iam-authorization-fixes, finding I18 / Req 2.18).

This is the companion gate to the sibling ``repo_audit.py`` (Group 1 —
injection / deserialization) and ``secrets_audit.py`` (Group 3 — secrets /
JWT). It owns a DIFFERENT set of patterns (over-broad IAM ``PolicyStatement``s:
wildcard resources on scopable actions, ``service:*`` action wildcards,
wildcard-account ``sts:AssumeRole`` grants, and comment-declared-but-unenforced
tag conditions) and a different in-scope file set, so it is deliberately a
separate module rather than an edit to the siblings' already-green gates. To
avoid duplication it REUSES the siblings' proven low-level primitives
(``REPO_ROOT``, ``EXCLUDE_DIRS``, ``EXCLUDED_PATH_SUBSTRING``, ``Hit``,
``_parse_line``, ``_is_comment_line``) and defines only its OWN
``AUDIT_PATTERNS``, ``IN_SCOPE_FILES``, and precise per-category
``_is_disallowed`` logic.

Two layers that intentionally differ in breadth (mirroring the siblings):

* ``run_audit()`` -- the RAW, broad, line-based enumeration. It scans the nine
  in-scope source files for every bug-condition token (``resources: ['*']`` in a
  CDK statement, ``"Resource": "*"`` / ``arn:aws:s3:::*`` in JSON, the
  ``service:*`` action wildcards, ``sts:AssumeRole`` + ``arn:aws:iam::*:role/``,
  the ``*-dda-*`` substring bucket pattern, and the ``dda-portal:managed`` tag
  comment). It applies NO precision / scoping filtering -- that is by design for
  the exploration phase. On the UNFIXED tree it surfaces NON-EMPTY hits across
  all four bug categories (the counterexamples that confirm the bug); task 1
  uses it to list them.

* ``disallowed_hits()`` -- the PRECISE post-fix gate re-run in task 8. It parses
  the CDK ``new iam.PolicyStatement({...})`` blocks (brace-matched) and the JSON
  policy documents (extracted from the shell heredocs, the standalone JSON
  template, and the README code fences, then ``json.loads``-parsed) and applies
  the exact design semantics so that ONLY a genuinely over-broad statement in
  in-scope source counts. Documented exceptions (a ``# nosec`` / ``// nosec``
  marker), the enumerated unscopable-action set (``s3:ListAllMyBuckets``,
  ``cloudwatch:PutMetricData``, ``sts:GetCallerIdentity``,
  ``logs:DescribeLogGroups``-class, ``sagemaker:ListWorkteams``,
  ``ecr:GetAuthorizationToken``, ``tag:GetResources`` /
  ``resourcegroupstaggingapi:GetResources``, ``iot:DescribeEndpoint``), and the
  legitimately-wildcarded non-finding statements (the Greengrass v1 service
  actions kept on ``*``, the ``GreengrassV2TokenExchangeRole`` /
  ``dda-cross-account-role`` device assume-role grants, the CloudWatch/EC2/ECR
  utility statements) are NOT disallowed. After the fix this must return zero.

Precise gate semantics (from design "Repo-audit design" / "Precise gate
semantics"):
  * ``cdk_wildcard_resource`` -- a ``new iam.PolicyStatement({...})`` block whose
    ``resources: ['*']`` sits alongside an ``actions:`` list containing a
    scopable action (SageMaker job/model, Greengrass, IoT, S3, execute-api,
    iam:PassRole, sts:AssumeRole). Disallowed unless every action is in the
    documented-unscopable set OR a ``// nosec`` marker is present.
  * ``json_resource_wildcard`` -- a JSON statement whose ``Resource`` is ``"*"``
    or an ``arn:aws:s3:::*`` bucket wildcard (incl. the ``*-dda-*`` substring
    pattern) alongside a scopable S3 / SageMaker / IoT-data-plane action.
    Disallowed unless a ``# nosec`` marker / ``"//"`` sibling key documents it.
  * ``service_wildcard_action`` -- ``greengrass:*`` / ``greengrassv2:*`` /
    ``iot:*`` / ``s3:*`` in an actions list. Disallowed unconditionally in
    in-scope files.
  * ``assume_role_wildcard_account`` -- ``sts:AssumeRole`` correlated with a full
    ``*`` resource or an ``arn:aws:iam::*:role/DDAPortalAccessRole`` account
    wildcard. Disallowed unless a ``// nosec`` marker is present.

In-scope scoping: the gate is asserted ONLY over ``IN_SCOPE_FILES`` (the nine
real source paths this spec owns), so it does NOT match the security
test/fixture files' own pattern strings, the generated
``edge-cv-portal/infrastructure/cdk.out/asset.*`` templates, or any vendored
duplicate. Precision + this scoping -- not a hard-coded line list -- is what
lets the gate still FAIL if a wildcard resource, a service-wildcard action, or a
wildcard-account ``sts:AssumeRole`` is reintroduced into any real source file.
"""
import json
import os
import re

# Reuse the sibling module's proven low-level primitives where sensible. If the
# import is unavailable in some runner, fall back to a thin re-implementation so
# this module stays self-contained.
try:
    from repo_audit import (  # type: ignore
        REPO_ROOT,
        EXCLUDE_DIRS,
        EXCLUDED_PATH_SUBSTRING,
        Hit,
        _parse_line,
        _is_comment_line,
    )
except Exception:  # pragma: no cover - fallback when repo_audit is not importable
    from collections import namedtuple

    REPO_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    )
    EXCLUDED_PATH_SUBSTRING = os.path.join("cdk.out", "asset.")
    EXCLUDE_DIRS = [
        ".git", "node_modules", ".hypothesis", "__pycache__", ".venv", "venv",
    ]
    Hit = namedtuple("Hit", ["category", "path", "lineno", "text"])

    def _parse_line(category, raw):
        m = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not m:
            return None
        return Hit(category=category, path=m.group(1),
                   lineno=int(m.group(2)), text=m.group(3))

    def _is_comment_line(text):
        return text.lstrip().startswith("#")


# ---------------------------------------------------------------------------
# In-scope files (relative to REPO_ROOT) -- the nine real source paths this
# spec owns, excluding vendored/generated copies (cdk.out/asset.*).
# ---------------------------------------------------------------------------
_CDK_TS_FILES = (
    os.path.join("edge-cv-portal", "infrastructure", "lib", "compute-stack.ts"),
    os.path.join("edge-cv-portal", "infrastructure", "lib", "usecase-account-stack.ts"),
    os.path.join("edge-cv-portal", "infrastructure", "lib", "labeling-workflow-stack.ts"),
    os.path.join("edge-cv-portal", "infrastructure", "lib", "training-workflow-stack.ts"),
)
_JSON_SRC_FILES = (
    os.path.join("edge-cv-portal", "deploy-account-role.sh"),
    os.path.join("station_install", "create-edge-device-iam-role.sh"),
    os.path.join("edge-cv-portal", "launch-arm64-build-server.sh"),
    os.path.join("station_install", "edge-device-iam-policy.json"),
    os.path.join("README_main.md"),
)
IN_SCOPE_FILES = frozenset(
    os.path.normpath(p) for p in (_CDK_TS_FILES + _JSON_SRC_FILES)
)

# The nine in-scope findings -> a path fragment that uniquely identifies the
# real source file (NOT the generated cdk.out/asset.* copies).
IN_SCOPE_SITES = {
    "I1/I2 compute-stack portal Lambda role": os.path.join(
        "edge-cv-portal", "infrastructure", "lib", "compute-stack.ts"),
    "I3/I4 usecase-account-stack S3 grants": os.path.join(
        "edge-cv-portal", "infrastructure", "lib", "usecase-account-stack.ts"),
    "I5 labeling-workflow-stack assume-role": os.path.join(
        "edge-cv-portal", "infrastructure", "lib", "labeling-workflow-stack.ts"),
    "I6 training-workflow-stack assume-role": os.path.join(
        "edge-cv-portal", "infrastructure", "lib", "training-workflow-stack.ts"),
    "I7/I8/I9 deploy-account-role.sh heredocs": os.path.join(
        "edge-cv-portal", "deploy-account-role.sh"),
    "I10/I11/I12 create-edge-device-iam-role.sh": os.path.join(
        "station_install", "create-edge-device-iam-role.sh"),
    "I13/I14 launch-arm64-build-server.sh": os.path.join(
        "edge-cv-portal", "launch-arm64-build-server.sh"),
    "I15 edge-device-iam-policy.json": os.path.join(
        "station_install", "edge-device-iam-policy.json"),
    "I16/I17 README_main.md example policies": os.path.join("README_main.md"),
}

# ---------------------------------------------------------------------------
# RAW enumeration patterns (line-based, applied to every in-scope file). These
# are deliberately broad -- the raw run_audit() layer surfaces every token so
# the exploration test can list the counterexamples per finding.
# ---------------------------------------------------------------------------
AUDIT_PATTERNS = [
    ("cdk_wildcard_resource", r"resources:\s*\[\s*['\"]\*['\"]"),
    ("json_resource_wildcard", r"\"Resource\"\s*:\s*\"\*\""),
    ("json_resource_wildcard", r"arn:aws:s3:::\*"),
    ("service_wildcard_action", r"['\"](?:greengrass|greengrassv2|iot|s3):\*['\"]"),
    ("assume_role_wildcard_account", r"sts:AssumeRole"),
    ("assume_role_wildcard_account", r"arn:aws:iam::\*:role/"),
    ("substring_bucket_pattern", r"arn:aws:s3:::\*-dda-\*"),
    ("unenforced_tag_condition", r"dda-portal:managed"),
]

# Actions the AWS IAM reference lists as genuinely un-scopable (must stay on a
# wildcard resource). A statement is exempt from cdk_wildcard_resource only when
# ALL its actions are wildcard-acceptable (this set OR the utility service
# families below).
UNSCOPABLE_ACTIONS = frozenset(a.lower() for a in (
    "sagemaker:ListWorkteams",
    "sts:GetCallerIdentity",
    "cloudwatch:PutMetricData",
    "logs:DescribeLogGroups",
    "ecr:GetAuthorizationToken",
    "tag:GetResources",
    "resourcegroupstaggingapi:GetResources",
    "iot:DescribeEndpoint",
    "s3:ListAllMyBuckets",
))

# The specific IoT data-plane / thing actions the fix scopes by resource type.
# (iot:DescribeEndpoint / CreateThing / AttachPolicy stay on "*" -- unscopable /
# out of the finding scope -- so they are NOT triggers.)
_IOT_SCOPABLE = frozenset(a.lower() for a in (
    "iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive",
    "iot:GetThingShadow", "iot:UpdateThingShadow", "iot:DescribeThing",
))


def _rel(path):
    return os.path.normpath(os.path.relpath(path, REPO_ROOT))


def _read(rel_path):
    """Read an in-scope file; return "" if it does not exist."""
    abs_path = os.path.join(REPO_ROOT, rel_path)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:  # pragma: no cover
        return ""


def _has_nosem(text):
    """True if the text carries a documented suppression marker."""
    low = text.lower()
    return "nosem" in low or "nosec" in low or "noqa" in low


# ---------------------------------------------------------------------------
# Layer 1 -- raw, broad, line-based enumeration.
# ---------------------------------------------------------------------------
def run_audit():
    """Return all raw audit Hits across the nine in-scope files
    (``cdk.out/asset.*`` never scanned). No scoping / exception filtering is
    applied -- this is the raw bug-condition enumeration used by the exploration
    test. Non-empty on the unfixed tree across all four bug categories."""
    hits = []
    for rel_path in sorted(IN_SCOPE_FILES):
        if EXCLUDED_PATH_SUBSTRING in rel_path:
            continue
        abs_path = os.path.join(REPO_ROOT, rel_path)
        text = _read(rel_path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for category, pattern in AUDIT_PATTERNS:
                if re.search(pattern, line):
                    hits.append(Hit(category=category, path=abs_path,
                                    lineno=lineno, text=line))
    return hits


# ---------------------------------------------------------------------------
# Scopable-action predicates.
# ---------------------------------------------------------------------------
def _cdk_action_is_scopable_trigger(action):
    """True if this action, when present in a CDK ``resources: ['*']`` block,
    marks the block over-broad. Utility service families (logs, cloudwatch, ec2,
    ecr, tag, cognito, ...) and the enumerated unscopable actions do NOT trigger,
    so non-finding wildcard statements are not falsely flagged."""
    low = action.lower()
    if low in UNSCOPABLE_ACTIONS:
        return False
    service, _, verb = low.partition(":")
    if service in ("sagemaker", "greengrass", "greengrassv2", "iot", "s3", "execute-api"):
        return True
    if service == "iam" and verb == "passrole":
        return True
    if service == "sts" and verb == "assumerole":
        return True
    return False


def _json_action_is_scopable_trigger(action):
    """True if this action, when present in a JSON statement whose Resource is a
    ``*`` / ``arn:aws:s3:::*`` wildcard, marks the statement over-broad. Only the
    services the fix actually scopes by resource in JSON count -- S3 (minus
    ListAllMyBuckets), SageMaker jobs/models (minus ListWorkteams), and the IoT
    data-plane subset. Greengrass stays on ``*`` (v1 resource support is limited)
    and so does NOT trigger here (its wildcards are caught, where applicable, by
    service_wildcard_action)."""
    low = action.lower()
    service, _, verb = low.partition(":")
    if service == "s3":
        return verb != "listallmybuckets"
    if service == "sagemaker":
        return verb != "listworkteams"
    if service == "iot":
        return low in _IOT_SCOPABLE
    return False


def _resource_list(resource_value):
    if isinstance(resource_value, str):
        return [resource_value]
    if isinstance(resource_value, list):
        return [r for r in resource_value if isinstance(r, str)]
    return []


def _json_resource_overbroad(resource_value):
    """True if a JSON ``Resource`` value is a full ``*`` or an
    ``arn:aws:s3:::*`` bucket-name wildcard (which also covers the ``*-dda-*``
    substring pattern). Prefixed ARNs (``dda-*``, ``sagemaker-*``,
    ``dda-component-*``) and non-S3 ARN wildcards (``arn:aws:logs:*:*:*``) are
    NOT over-broad."""
    for r in _resource_list(resource_value):
        if r == "*":
            return True
        if re.match(r"arn:aws:s3:::\*", r):
            return True
    return False


def _action_list(action_value):
    if isinstance(action_value, str):
        return [action_value]
    if isinstance(action_value, list):
        return [a for a in action_value if isinstance(a, str)]
    return []


def _assume_role_resource_wildcard(resource_value):
    """True if an ``sts:AssumeRole`` resource is a full ``*`` or the
    ``DDAPortalAccessRole`` account wildcard the findings target. The Greengrass
    ``GreengrassV2TokenExchangeRole*`` and ``dda-cross-account-role`` device
    grants (standard, account-unknown-at-write-time patterns; not findings) are
    NOT flagged."""
    for r in _resource_list(resource_value):
        if r == "*":
            return True
        if re.match(r"arn:aws:iam::\*:role/DDAPortalAccessRole", r):
            return True
    return False


# ---------------------------------------------------------------------------
# CDK PolicyStatement block parsing.
# ---------------------------------------------------------------------------
def _match_bracket(text, open_idx, open_ch, close_ch):
    """Return the index of the bracket matching ``text[open_idx]``, respecting
    quoted strings (', ", `) and // line comments. Returns -1 if unbalanced."""
    depth = 0
    i = open_idx
    n = len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return -1
            i = nl
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _iter_cdk_statement_blocks(text):
    """Yield (block_text, start_line) for each ``new iam.PolicyStatement({...})``
    object literal in a CDK source file."""
    for m in re.finditer(r"new\s+iam\.PolicyStatement\s*\(", text):
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        close = _match_bracket(text, brace, "{", "}")
        if close == -1:
            continue
        block = text[brace:close + 1]
        start_line = text.count("\n", 0, brace) + 1
        yield block, start_line


def _block_string_array(block, key):
    """Return the quoted string entries of an ``actions:`` / ``resources:``
    array in a CDK statement block."""
    m = re.search(key + r"\s*:\s*\[(.*?)\]", block, re.DOTALL)
    if not m:
        return []
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))


def _block_resources_have_wildcard(block):
    return re.search(r"resources\s*:\s*\[\s*['\"]\*['\"]", block) is not None


# The dda-portal:managed resource tag that makes an S3 bucket-name wildcard
# (arn:aws:s3:::*) safe: access is gated to portal-managed buckets only (I3/I4).
_MANAGED_TAG = "aws:ResourceTag/dda-portal:managed"


def _block_s3_bucket_wildcards(block):
    """Return the ``arn:aws:s3:::*`` bucket-name wildcard entries in the block's
    ``resources:`` array (covers ``arn:aws:s3:::*`` and ``arn:aws:s3:::*/*``).
    These are scopable S3 wildcards -- unlike a prefixed ``arn:aws:s3:::dda-*``
    ARN, an ``arn:aws:s3:::*`` grants every bucket in the account."""
    return [r for r in _block_string_array(block, "resources")
            if re.match(r"arn:aws:s3:::\*", r)]


def _block_has_managed_tag_condition(block):
    """True if the CDK statement block carries a ``conditions:`` block that
    references the ``dda-portal:managed`` resource tag. This is the I3/I4
    exemption: a ``arn:aws:s3:::*`` wildcard gated by
    ``aws:ResourceTag/dda-portal:managed = true`` is legitimately safe because
    the Condition restricts it to portal-managed buckets. Dropping the
    ``conditions:`` block removes the exemption and the wildcard is flagged
    again."""
    return "conditions" in block and _MANAGED_TAG in block


def _s3_action_is_scopable_trigger(action):
    """True if this S3 action, when present alongside an ``arn:aws:s3:::*``
    resource, marks the statement over-broad. ``s3:ListAllMyBuckets`` genuinely
    does not support resource-level permissions, so it is not a trigger."""
    low = action.lower()
    service, _, verb = low.partition(":")
    return service == "s3" and verb != "listallmybuckets"


# ---------------------------------------------------------------------------
# JSON policy-document extraction (shell heredocs / JSON template / README).
# ---------------------------------------------------------------------------
def _substitute_shell_vars(s):
    """Replace shell ``${VAR}`` / ``$VAR`` interpolations with a placeholder so
    the heredoc body parses as JSON."""
    s = re.sub(r"\$\{[^}]*\}", "PLACEHOLDER", s)
    s = re.sub(r"\$[A-Za-z_][A-Za-z0-9_]*", "PLACEHOLDER", s)
    return s


def _iter_json_policies(text):
    """Yield (policy_dict, start_line) for each IAM policy document embedded in a
    file. A policy document is located by its ``"Version"`` key; the enclosing
    object is brace-matched from the nearest preceding ``{``."""
    for m in re.finditer(r"\"Version\"\s*:\s*\"2012-10-17\"", text):
        brace = text.rfind("{", 0, m.start())
        if brace == -1:
            continue
        close = _match_bracket(text, brace, "{", "}")
        if close == -1:
            continue
        raw = text[brace:close + 1]
        start_line = text.count("\n", 0, brace) + 1
        try:
            policy = json.loads(_substitute_shell_vars(raw))
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(policy, dict) and "Statement" in policy:
            yield policy, start_line


def _statement_line(text, statement, fallback_line):
    """Best-effort line number for a JSON statement (by its Sid), else the
    policy's start line."""
    sid = statement.get("Sid")
    if sid:
        m = re.search(r"\"Sid\"\s*:\s*\"" + re.escape(sid) + r"\"", text)
        if m:
            return text.count("\n", 0, m.start()) + 1
    return fallback_line


# ---------------------------------------------------------------------------
# Layer 2 -- precise post-fix gate.
# ---------------------------------------------------------------------------
def _cdk_disallowed(rel_path, text):
    """Precise cdk_wildcard_resource + assume_role_wildcard_account +
    service_wildcard_action checks for a CDK source file."""
    hits = []
    abs_path = os.path.join(REPO_ROOT, rel_path)
    for block, start_line in _iter_cdk_statement_blocks(text):
        actions = _block_string_array(block, "actions")
        low_actions = [a.lower() for a in actions]
        documented = _has_nosem(block)

        # service_wildcard_action -- greengrass:* / iot:* / s3:* in actions.
        for a in actions:
            if re.match(r"(greengrass|greengrassv2|iot|s3):\*$", a):
                hits.append(Hit("service_wildcard_action", abs_path, start_line,
                                f"CDK PolicyStatement actions include '{a}'"))

        has_wildcard_resource = _block_resources_have_wildcard(block)

        # cdk_wildcard_resource -- resources: ['*'] alongside a scopable action.
        if has_wildcard_resource and not documented:
            triggers = [a for a in actions if _cdk_action_is_scopable_trigger(a)]
            if triggers:
                hits.append(Hit(
                    "cdk_wildcard_resource", abs_path, start_line,
                    "CDK PolicyStatement grants resources: ['*'] with scopable "
                    f"action(s) {triggers[:3]}"))

        # cdk_wildcard_resource (S3 bucket-name wildcard) -- an arn:aws:s3:::*
        # resource alongside a scopable S3 action. This POSITIVELY VALIDATES the
        # I3/I4 tag-conditioned wildcards: the statement is DISALLOWED unless it
        # is gated by the dda-portal:managed tag Condition (the legitimate I3/I4
        # exemption) or documented via a // nosec marker. If someone drops the
        # I3/I4 conditions: block, the wildcard is flagged again -- so a
        # regression cannot silently sneak past the gate.
        s3_wildcards = _block_s3_bucket_wildcards(block)
        if (s3_wildcards and not documented
                and not _block_has_managed_tag_condition(block)):
            s3_triggers = [a for a in actions if _s3_action_is_scopable_trigger(a)]
            if s3_triggers:
                hits.append(Hit(
                    "cdk_wildcard_resource", abs_path, start_line,
                    "CDK PolicyStatement grants scopable S3 action(s) "
                    f"{s3_triggers[:3]} on over-broad S3 resource "
                    f"{s3_wildcards} with no dda-portal:managed tag Condition"))

        # assume_role_wildcard_account -- sts:AssumeRole + wildcard account.
        if "sts:assumerole" in low_actions and not documented:
            resources = _block_string_array(block, "resources")
            if _assume_role_resource_wildcard(resources):
                hits.append(Hit(
                    "assume_role_wildcard_account", abs_path, start_line,
                    "CDK PolicyStatement grants sts:AssumeRole on a wildcard "
                    f"account resource {resources}"))
    return hits


def _json_disallowed(rel_path, text):
    """Precise json_resource_wildcard + service_wildcard_action +
    assume_role_wildcard_account checks for a JSON-bearing source file."""
    hits = []
    abs_path = os.path.join(REPO_ROOT, rel_path)
    for policy, start_line in _iter_json_policies(text):
        statements = policy.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        for stmt in statements:
            if not isinstance(stmt, dict):
                continue
            sid = stmt.get("Sid", "<no-sid>")
            line = _statement_line(text, stmt, start_line)
            actions = _action_list(stmt.get("Action"))
            resource = stmt.get("Resource")
            documented = "//" in stmt  # JSON-adjacent documented-exception key

            # service_wildcard_action.
            for a in actions:
                if re.match(r"(greengrass|greengrassv2|iot|s3):\*$", a):
                    hits.append(Hit("service_wildcard_action", abs_path, line,
                                    f"JSON statement '{sid}' Action includes '{a}'"))

            # json_resource_wildcard.
            if not documented and _json_resource_overbroad(resource):
                triggers = [a for a in actions if _json_action_is_scopable_trigger(a)]
                if triggers:
                    hits.append(Hit(
                        "json_resource_wildcard", abs_path, line,
                        f"JSON statement '{sid}' grants scopable action(s) "
                        f"{triggers[:3]} on over-broad Resource {resource!r}"))

            # assume_role_wildcard_account.
            if not documented and any(a.lower() == "sts:assumerole" for a in actions):
                if _assume_role_resource_wildcard(resource):
                    hits.append(Hit(
                        "assume_role_wildcard_account", abs_path, line,
                        f"JSON statement '{sid}' grants sts:AssumeRole on a "
                        f"wildcard account resource {resource!r}"))
    return hits


def disallowed_hits():
    """The PRECISE post-fix pattern gate (task 8).

    A statement is *disallowed* only when it is in in-scope source, carries no
    documented ``# nosec`` / ``// nosec`` / ``"//"`` exception, and matches the
    precise design semantics for one of the four bug categories
    (cdk_wildcard_resource, json_resource_wildcard, service_wildcard_action,
    assume_role_wildcard_account).

    On the UNFIXED tree this is non-empty (the I1-I17 counterexamples); after the
    fix it must be empty (minus documented exceptions)."""
    hits = []
    for rel_path in sorted(_CDK_TS_FILES):
        text = _read(rel_path)
        if text:
            hits.extend(_cdk_disallowed(rel_path, text))
    for rel_path in sorted(_JSON_SRC_FILES):
        text = _read(rel_path)
        if text:
            hits.extend(_json_disallowed(rel_path, text))
    return hits


def hits_for(path_substring, hits=None):
    """All hits whose file path contains ``path_substring``."""
    hits = run_audit() if hits is None else hits
    return [h for h in hits if path_substring in h.path]


def disallowed_by_category(hits=None):
    """Group disallowed hits by category (for the exploration test's
    all-four-categories assertion)."""
    hits = disallowed_hits() if hits is None else hits
    grouped = {}
    for h in hits:
        grouped.setdefault(h.category, []).append(h)
    return grouped


if __name__ == "__main__":
    all_hits = run_audit()
    print(f"IAM audit: {len(all_hits)} raw bug-condition hits "
          f"(cdk.out/asset.* excluded)\n")
    for label, frag in IN_SCOPE_SITES.items():
        site_hits = hits_for(frag, all_hits)
        print(f"=== {label} ({frag}) : {len(site_hits)} raw hit(s) ===")
        for h in site_hits:
            print(f"  [{h.category}] {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
        print()

    disallowed = disallowed_hits()
    grouped = disallowed_by_category(disallowed)
    print("-" * 70)
    print(f"PATTERN GATE: {len(disallowed)} disallowed hit(s) in in-scope "
          f"source (must be 0 after fix, minus documented exceptions).")
    for category in sorted(grouped):
        print(f"  --- {category}: {len(grouped[category])} hit(s) ---")
        for h in grouped[category]:
            print(f"    {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
    raise SystemExit(1 if disallowed else 0)
