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
"""Shared helpers for the **IAM & Authorization** preservation baseline tests
(Task 2 of ``security-iam-authorization-fixes``).

These tests implement **Property 2: Preservation — F(X) = F'(X) for every
legitimate (non-bug-condition) input** (``bugfix.md`` Req 3.1–3.18, ``design.md``
"Preservation Checking"). Methodology: observation-first — capture the baseline
behavior on the UNFIXED tree (task 2, PASS now), then re-run the SAME files
against the FIXED tree (task 9) to prove no legitimate behavior changed.

This module provides the pure extraction helpers the baseline tests rely on:

* ``extract_heredoc_json`` — pulls an inline ``VAR=$(cat <<EOF ... EOF)`` heredoc
  JSON body out of a shell installer, substitutes shell-var placeholders with a
  fixture value, and parses it as JSON.
* ``extract_inline_policy_document`` — pulls a single-quoted
  ``--policy-document '{...}'`` payload (anchored on a preceding
  ``--policy-name NAME``) out of a shell installer and parses it as JSON.
* ``extract_ts_policy_block`` — extracts a ``new iam.PolicyStatement({...})``
  source block from a CDK ``.ts`` file by an anchor substring (source-level
  baseline used for the two workflow stacks that are not wired into a
  synthesizable CDK app entrypoint).
* ``iam_statements_from_template`` — walks a synthesized CloudFormation template
  and returns a canonical, deterministic view of every ``AWS::IAM::Role`` /
  ``AWS::IAM::Policy`` / ``AWS::IAM::ManagedPolicy`` PolicyDocument (used by the
  ``cdk synth`` preservation diff for I1–I4).
* ``extract_readme_fence`` / ``readme_prose_without_fences`` — README JSON code
  fence handling for the I16 / I17 prose baseline.

All helpers are import-light so the tests run under
``python3 -m pytest ... --noconftest`` without pulling in the backend package.
"""
import json
import re

# Sibling module already committed by security-injection-deserialization-fixes.
from _preservation_support import REPO_ROOT, read_repo_file  # noqa: F401

# Fixture value substituted for the shell ``$CURRENT_ACCOUNT`` /
# ``${CURRENT_ACCOUNT}`` placeholder before ``json.loads`` — deploy-account-role.sh
# uses an UNQUOTED heredoc, so the account id is interpolated at runtime. A fixed
# 12-digit value makes the parsed baseline deterministic.
FIXTURE_ACCOUNT = "123456789012"


# --------------------------------------------------------------------------- #
# Shell heredoc / inline-policy extraction (I7–I14)
# --------------------------------------------------------------------------- #
def _substitute_shell_placeholders(text):
    """Replace the shell variable placeholders that appear inside the unquoted
    heredocs with the fixture account id so the body parses as valid JSON."""
    text = text.replace("${CURRENT_ACCOUNT}", FIXTURE_ACCOUNT)
    text = text.replace("$CURRENT_ACCOUNT", FIXTURE_ACCOUNT)
    return text


def extract_heredoc_body(script_text, varname):
    """Return the raw body of a ``VARNAME=$(cat <<EOF ... EOF)`` (or
    ``<<'EOF'``) heredoc assignment, verbatim (no placeholder substitution)."""
    m = re.search(
        r"^[ \t]*" + re.escape(varname) + r"=\$\(cat <<'?(?P<delim>\w+)'?[ \t]*\n",
        script_text,
        re.MULTILINE,
    )
    if not m:
        raise AssertionError(f"heredoc assignment for {varname!r} not found")
    delim = m.group("delim")
    start = m.end()
    term = re.search(r"^" + re.escape(delim) + r"[ \t]*$", script_text[start:], re.MULTILINE)
    if not term:
        raise AssertionError(f"heredoc terminator {delim!r} for {varname!r} not found")
    return script_text[start : start + term.start()]


def extract_heredoc_json(script_text, varname):
    """Extract a heredoc body and parse it as JSON (placeholders substituted)."""
    body = _substitute_shell_placeholders(extract_heredoc_body(script_text, varname))
    return json.loads(body)


def extract_inline_policy_document(script_text, policy_name):
    """Extract the single-quoted ``--policy-document '{...}'`` JSON payload that
    follows a ``--policy-name POLICY_NAME`` token, and parse it as JSON.

    The closing delimiter is the brace-quote pair ``}'`` — inside the JSON body a
    ``}`` is always followed by a newline/comma, never by a single quote, so the
    non-greedy match terminates only at the true end of the document.
    """
    anchor = script_text.index("--policy-name " + policy_name)
    m = re.search(r"--policy-document '(\{.*?\})'", script_text[anchor:], re.DOTALL)
    if not m:
        raise AssertionError(
            f"inline --policy-document for {policy_name!r} not found"
        )
    body = _substitute_shell_placeholders(m.group(1))
    return json.loads(body)


# --------------------------------------------------------------------------- #
# CDK .ts source-block extraction (source-level baseline for I5 / I6, and the
# targeted I1–I4 blocks recorded for documentation / fallback)
# --------------------------------------------------------------------------- #
def extract_ts_policy_block(ts_text, anchor_substring):
    """Return the ``new iam.PolicyStatement({ ... })`` source block whose body
    contains ``anchor_substring``.

    Brace-matching from the ``new iam.PolicyStatement(`` that precedes the anchor
    up to its balanced close, so the returned text is the exact statement block
    as written in source (whitespace preserved). This is the source-level
    baseline for the two workflow stacks that are not instantiated in any
    synthesizable CDK app entrypoint.
    """
    anchor_idx = ts_text.index(anchor_substring)
    open_idx = ts_text.rfind("new iam.PolicyStatement(", 0, anchor_idx)
    if open_idx == -1:
        raise AssertionError(
            f"no PolicyStatement precedes anchor {anchor_substring!r}"
        )
    # Walk forward from the first '{' after the constructor, matching braces.
    brace_start = ts_text.index("{", open_idx)
    depth = 0
    i = brace_start
    while i < len(ts_text):
        c = ts_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return ts_text[open_idx : i + 1]
        i += 1
    raise AssertionError(f"unbalanced PolicyStatement block for {anchor_substring!r}")


# --------------------------------------------------------------------------- #
# Synthesized CloudFormation IAM extraction (I1–I6 cdk synth diff)
# --------------------------------------------------------------------------- #
def iam_statements_multiset(template):
    """Return a ``collections.Counter`` of canonical (sorted-key JSON) IAM
    statement strings across every ``AWS::IAM::Policy``, ``AWS::IAM::ManagedPolicy``
    and inline ``AWS::IAM::Role`` ``Policies`` PolicyDocument in a synthesized
    template.

    Unlike :func:`iam_statements_from_template` (which groups by logical id and
    is sensitive to how CDK distributes statements across the auto-generated
    ``DefaultPolicy`` / ``OverflowPolicy`` / managed-policy carriers), this view
    is a flat multiset of the actual *statements granted*. That makes it stable
    against the statement-carrier reshuffling CDK performs when a role's inline
    policy grows past the managed-policy size limit — which happens precisely
    because the I1 fix splits one broad statement into many narrow ones. The
    preservation invariant we care about (``F(X) = F'(X)`` for every non-I1–I6
    statement) is expressed exactly at this granularity: the multiset difference
    between the unfixed and fixed templates must contain ONLY the enumerated
    I1–I6 statement rewrites.
    """
    from collections import Counter

    counter = Counter()
    resources = template.get("Resources", {})
    for res in resources.values():
        ty = res.get("Type", "")
        props = res.get("Properties", {})
        if ty in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            pd = props.get("PolicyDocument")
            if pd is not None:
                for stmt in pd.get("Statement", []) or []:
                    counter[json.dumps(stmt, sort_keys=True)] += 1
        elif ty == "AWS::IAM::Role":
            for pol in props.get("Policies", []) or []:
                pd = pol.get("PolicyDocument")
                if pd is not None:
                    for stmt in pd.get("Statement", []) or []:
                        counter[json.dumps(stmt, sort_keys=True)] += 1
    return counter


def iam_statements_from_template(template):
    """Return a deterministic, canonical view of every IAM PolicyDocument in a
    synthesized CloudFormation template.

    The result is a list of ``[logicalId, resourceType, policyName,
    canonical_policy_document_json]`` tuples sorted by logical id then policy
    name — stable across synth runs (IAM PolicyDocuments do not carry asset
    hashes or timestamps). Only ``AWS::IAM::Role`` (inline ``Policies`` and the
    ``AssumeRolePolicyDocument``), ``AWS::IAM::Policy`` and
    ``AWS::IAM::ManagedPolicy`` resources contribute.
    """
    out = []
    resources = template.get("Resources", {})
    for lid in sorted(resources):
        res = resources[lid]
        ty = res.get("Type", "")
        props = res.get("Properties", {})
        if ty == "AWS::IAM::Policy" or ty == "AWS::IAM::ManagedPolicy":
            pd = props.get("PolicyDocument")
            if pd is not None:
                out.append([lid, ty, "<inline>", json.dumps(pd, sort_keys=True)])
        elif ty == "AWS::IAM::Role":
            arpd = props.get("AssumeRolePolicyDocument")
            if arpd is not None:
                out.append(
                    [lid, ty, "<assume-role>", json.dumps(arpd, sort_keys=True)]
                )
            for pol in props.get("Policies", []) or []:
                out.append(
                    [
                        lid,
                        ty,
                        pol.get("PolicyName", "<unnamed>"),
                        json.dumps(pol.get("PolicyDocument"), sort_keys=True),
                    ]
                )
    out.sort(key=lambda r: (r[0], str(r[2])))
    return out


# --------------------------------------------------------------------------- #
# README JSON code fence handling (I16 / I17)
# --------------------------------------------------------------------------- #
# The two example policies are the ONLY thing this spec changes in README_main.md.
# For I16 the fence is preceded by an indented ```json ... ``` block; for I17 the
# fence sits at column 0. We locate each fenced JSON block by the first line of
# its JSON body and excise the WHOLE fence (from the opening ```json to the
# closing ```), leaving the surrounding prose to be compared byte-for-byte.
FENCE_RE = re.compile(
    r"[ \t]*```json[ \t]*\n(?P<body>.*?)\n[ \t]*```[ \t]*",
    re.DOTALL,
)


def iter_json_fences(md_text):
    """Yield ``(match, parsed_json_or_None)`` for every ```json fenced block."""
    for m in FENCE_RE.finditer(md_text):
        try:
            parsed = json.loads(m.group("body"))
        except json.JSONDecodeError:
            parsed = None
        yield m, parsed


def readme_prose_excise_all_json_fences(md_text, token="<<<IAM_JSON_FENCE_EXCISED>>>"):
    """Return ``md_text`` with EVERY ```json fenced block replaced by ``token``.

    Signature-agnostic version of :func:`readme_prose_without_fences`. The I16 /
    I17 fix rewrites the JSON *bodies* of the two example-policy fences (e.g. it
    removes the ``greengrass:*`` wildcard), so a body-substring signature is
    fragile — it stops matching once the fix lands. ``README_main.md`` contains
    exactly the two IAM example-policy ```json fences and no other ```json
    blocks, so excising every ```json fence holds out precisely those two policy
    bodies and leaves the surrounding prose (which MUST stay byte-for-byte
    identical) intact for comparison.
    """
    return FENCE_RE.sub(token, md_text)


def readme_prose_without_fences(md_text, fence_signatures):
    """Return ``md_text`` with every ```json fence whose body contains one of the
    ``fence_signatures`` substrings replaced by a stable placeholder token, so the
    surrounding prose can be compared byte-for-byte while the JSON bodies (which
    this spec rewrites) are held out.
    """
    def _replace(m):
        body = m.group("body")
        for sig in fence_signatures:
            if sig in body:
                return "<<<IAM_JSON_FENCE_EXCISED>>>"
        return m.group(0)

    return FENCE_RE.sub(_replace, md_text)
