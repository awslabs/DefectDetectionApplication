"""Property test for Setup_Command well-formedness (station-quick-setup task 3.5).

**Feature: station-quick-setup, Property 4: The Setup_Command is well formed**

For any created or regenerated Device_Registration, the returned Setup_Command
is a single line (no newline characters) of at most 2048 characters, contains
the Setup_Token and the generating deployment's Quick_Setup_Endpoint URL, and
every URL appearing in it uses the ``https://`` scheme.

**Validates: Requirements 2.1, 2.2, 2.3**

`device_registrations._build_setup_command(event, token)` is the single
command-construction path used by both the creation route
(`create_registration`) and the regeneration route, so exercising it across
arbitrary deployment request contexts and real Setup_Tokens covers both the
"created" and "regenerated" cases named in the property. The endpoint is
derived from the incoming API Gateway request context
(`requestContext.domainName` + `stage`), exactly as in production, so the test
pins the real deployment-URL derivation rather than a stubbed value. Tokens are
produced by the real `token_service.generate_token`.
"""
from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

import device_registrations as dr
import token_service as ts

# Requirement 2.2 upper bound, stated independently of the module constant so
# the test pins the 2048-character contract rather than echoing the code.
MAX_COMMAND_LENGTH = 2048

# Matches any http/https URL token up to the next whitespace or quote — the
# command separates its two URLs from following flags/args with a space.
_URL_RE = re.compile(r'https?://[^\s"\']+')

# A single DNS label: alphanumerics and hyphens, not starting/ending with a
# hyphen. Kept short so realistic multi-label domains stay well within limits.
_dns_label = st.from_regex(r'[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?', fullmatch=True)

# Realistic API Gateway custom / execute-api domain names: 1-4 dotted labels.
domain_names = st.lists(_dns_label, min_size=1, max_size=4).map('.'.join)

# API Gateway stage names are short url-safe tokens; also cover the
# no-stage case (some custom-domain base-path mappings omit the stage).
stage_names = st.one_of(
    st.none(),
    st.from_regex(r'[a-zA-Z0-9_-]{1,20}', fullmatch=True),
)

# Registration ids are uuid4 strings in production; the token wire format only
# forbids the "." separator inside a field. Cover a representative range.
registration_ids = st.from_regex(r'[a-zA-Z0-9_-]{1,40}', fullmatch=True)

# Bootstrap SHA-256 baked into the Lambda env at deploy time (64 lowercase hex)
# plus the empty default that an un-baked environment would expose.
bootstrap_shas = st.one_of(
    st.just(''),
    st.from_regex(r'[0-9a-f]{64}', fullmatch=True),
)


def _make_event(domain_name, stage):
    """A minimal API Gateway proxy event carrying only the request-context
    fields `_build_setup_command` -> `_api_base_url` reads."""
    request_context = {'domainName': domain_name}
    if stage is not None:
        request_context['stage'] = stage
    return {'requestContext': request_context}


@settings(max_examples=200, deadline=None)
@given(
    domain_name=domain_names,
    stage=stage_names,
    registration_id=registration_ids,
    bootstrap_sha=bootstrap_shas,
)
def test_setup_command_is_well_formed(domain_name, stage, registration_id,
                                      bootstrap_sha):
    """**Feature: station-quick-setup, Property 4: The Setup_Command is well formed**

    For any deployment request context and any issued Setup_Token, the built
    Setup_Command is one line, at most 2048 chars, embeds the token and the
    deployment's Quick_Setup_Endpoint URL, and uses only https URLs.

    **Validates: Requirements 2.1, 2.2, 2.3**
    """
    # The bootstrap checksum is a per-deployment module constant; set it for
    # this example so we exercise the real command template (Req 4.8 anchor).
    dr.QUICK_SETUP_BOOTSTRAP_SHA256 = bootstrap_sha

    # A real Setup_Token via the production issuance path (Req 2.1).
    token, _token_hash, _expires_at = ts.generate_token(registration_id)

    event = _make_event(domain_name, stage)
    command = dr._build_setup_command(event, token)

    # The deployment's Quick_Setup_Endpoint URL, derived exactly as the module
    # derives it from the request context.
    base = f"https://{domain_name}"
    if stage:
        base = f"{base}/{stage}"
    expected_endpoint = f"{base}/quick-setup"

    # 1. Single line: no newline or carriage-return characters (Req 2.2).
    assert '\n' not in command
    assert '\r' not in command

    # 2. At most 2048 characters (Req 2.2).
    assert len(command) <= MAX_COMMAND_LENGTH

    # 3. Embeds the Setup_Token (Req 2.1).
    assert token in command

    # 4. Embeds the generating deployment's Quick_Setup_Endpoint URL (Req 2.1).
    assert expected_endpoint in command

    # 5. Every URL appearing in the command uses the https scheme (Req 2.3),
    #    and at least one URL is present so the check is non-vacuous.
    urls = _URL_RE.findall(command)
    assert urls, "expected the Setup_Command to contain at least one URL"
    for url in urls:
        assert url.startswith('https://'), f"non-https URL in command: {url}"


def test_setup_command_concrete_example():
    """A concrete realistic deployment yields a copy-pasteable one-liner that
    embeds the endpoint and token over https (Req 2.1-2.3)."""
    dr.QUICK_SETUP_BOOTSTRAP_SHA256 = 'a' * 64
    token, _h, _e = ts.generate_token('11111111-2222-3333-4444-555555555555')
    event = _make_event('abc123.execute-api.us-east-1.amazonaws.com', 'v1')
    command = dr._build_setup_command(event, token)

    assert '\n' not in command and '\r' not in command
    assert len(command) <= MAX_COMMAND_LENGTH
    assert token in command
    assert 'https://abc123.execute-api.us-east-1.amazonaws.com/v1/quick-setup' in command
    for url in _URL_RE.findall(command):
        assert url.startswith('https://')
