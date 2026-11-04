"""Property 8: Backend status mapping and scrubbing (portal clauses).

**Feature: mcp-servers, Property 8: Backend status mapping and scrubbing**

Spec: .kiro/specs/mcp-servers/ (design.md Correctness Properties, Property 8;
§2 `errors.py` and `shaping.py` rows, D36, Error Handling "Backend status"
and "Use_Case binding"; tasks.md 6.6). **Validates: Requirements 1.5, 2.7**

For any backend status and body, `map_backend_status` yields the category of
the mapping table and a message without traceback markers or the token; for
a `bind_usecase_path` spec a backend 404 gives the same whole
`CallToolResult` as the D36 refusal of a 2xx body, except `request_id`.

Extended by .kiro/specs/mcp-user-operations/ (Property 5, tasks.md 2.11):
**Feature: mcp-user-operations, Property 5: Backend status mapping with the
detail key** - without `detail_key` the mapping is the baseline's; with
`detail_key='message'` the handler's `message` is joined to its `error`.
"""
import base64
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_portal import catalog as cat
from mcp_portal import errors, shaping
from mcp_portal.dispatch import BackendResponse

SETTINGS = settings(max_examples=100, deadline=None)

# design.md Error Handling, "Backend status" row.
TABLE = {400: 'invalid_input', 422: 'invalid_input', 401: 'not_authenticated',
         403: 'not_authorized', 404: 'not_found', 409: 'conflict', 429: 'throttled'}
BOUND = [s for s in cat.CATALOG if s.bind_usecase_path]

tokens = st.builds(
    lambda header, payload, signature: '.'.join(
        base64.urlsafe_b64encode(part).decode().rstrip('=')
        for part in (b'{"alg":"RS256"' + header + b'}', b'{"sub":"' + payload + b'"}',
                     signature)),
    st.binary(max_size=8), st.binary(min_size=1, max_size=16).map(bytes.hex).map(str.encode),
    st.binary(min_size=16, max_size=48))

TRACEBACK = 'Traceback (most recent call last):\n  File "/var/task/handler.py", line 7, in h'


@st.composite
def bodies(draw, token):
    fragments = draw(st.lists(st.one_of(
        st.text(max_size=30),
        st.just(TRACEBACK),
        st.just('File "/var/task/x.py"'),
        st.just(f'Bearer {token}'),
        st.just(token),
        st.just(f'Authorization: Bearer {token} ')), max_size=4))
    message = ''.join(fragments)
    return draw(st.one_of(
        st.just({'error': message}),
        st.just({'message': message}),
        st.just({'detail': {'message': message}}),
        st.just({'error': {'error': message}, 'other': token}),
        st.just(json.dumps({'error': message})),
        st.just(message),
        st.none(),
        st.recursive(st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10)),
                     lambda inner: st.one_of(st.lists(inner, max_size=3),
                                             st.dictionaries(st.text(max_size=5), inner,
                                                             max_size=3)),
                     max_leaves=8)))


statuses = st.one_of(st.sampled_from(sorted(TABLE)), st.integers(500, 599),
                     st.integers(400, 499), st.integers(300, 399))


@SETTINGS
@given(data=st.data(), status=statuses, token=tokens)
def test_status_mapping_and_scrubbing(data, status, token):
    body = data.draw(bodies(token))
    failure = errors.map_backend_status(status, body)
    if status in TABLE:
        assert failure.category == TABLE[status]
    elif 500 <= status <= 599:
        assert failure.category == 'backend_failure'
    else:
        assert failure.category in errors.CATEGORIES
    assert failure.status == status
    payload, signature = token.split('.')[1:]
    for text in (failure.message, failure.text):
        assert 'Traceback' not in text and 'File "' not in text
        assert token not in text
        # Two adjacent tokens are both removed, not only the first.
        assert f'{payload}.{signature}' not in text
    rendered = json.dumps(failure.to_result('req').model_dump(mode='json'))
    assert token not in rendered and 'Traceback' not in rendered


def baseline_mapping(status, body) -> errors.ToolFailure:
    """`map_backend_status` as it was before the detail key."""
    category = errors.category_for(int(status))
    return errors.ToolFailure(category, errors.scrub(errors._error_text(body), category),
                              status=int(status))


@st.composite
def detail_bodies(draw, token):
    """An object body whose `error` and `message` are each absent, a string
    (possibly with a traceback marker or the token), or another value."""
    text = st.lists(st.one_of(st.text(max_size=40), st.just(TRACEBACK),
                              st.just(f'Bearer {token}'), st.just(token),
                              st.just('x' * 700)),
                    max_size=3).map(''.join)
    value = st.one_of(text, st.none(), st.integers(), st.booleans())
    body = {}
    for key in ('error', 'message'):
        if draw(st.booleans()):
            body[key] = draw(value)
    if 'error' in body and isinstance(body['error'], str) and draw(st.booleans()):
        body['message'] = body['error']          # the same text twice
    return body


@SETTINGS
@given(data=st.data(), status=statuses, token=tokens)
def test_status_mapping_with_the_detail_key(data, status, token):
    """**Feature: mcp-user-operations, Property 5: Backend status mapping with the detail key**

    Validates: Requirements 3.4, 3.5, 3.6, 5.8 of .kiro/specs/mcp-user-operations/."""
    body = data.draw(st.one_of(detail_bodies(token), bodies(token)))
    plain = errors.map_backend_status(status, body)
    assert plain == baseline_mapping(status, body)
    assert plain.text == baseline_mapping(status, body).text
    joined = errors.map_backend_status(status, body, detail_key='message')
    assert joined.category == plain.category and joined.status == plain.status
    # The rule (design A7): a non-empty string at the key, different from
    # the error text, is joined to it; anything else leaves the baseline.
    text = errors._error_text(body)
    detail = body.get('message') if isinstance(body, dict) else None
    if isinstance(detail, str) and detail and detail != text:
        expected = f'{text}: {detail}' if text else detail
    else:
        expected = text
    assert joined.message == errors.scrub(expected, joined.category)
    # The clauses of Property 5, case by case.
    if isinstance(body, dict):
        error, message = body.get('error'), body.get('message')
        error_is_text = isinstance(error, str) and bool(error)
        message_is_text = isinstance(message, str) and bool(message)
        if 'error' not in body and message_is_text:
            assert joined.message == errors.scrub(message, joined.category)
        elif error_is_text and message_is_text and message != error:
            assert joined.message == errors.scrub(f'{error}: {message}', joined.category)
        elif error_is_text:
            assert joined.message == plain.message
    else:
        assert joined.message == plain.message
    payload, signature = token.split('.')[1:]
    for text in (joined.message, joined.text):
        assert 'Traceback' not in text and 'File "' not in text
        assert token not in text and f'{payload}.{signature}' not in text
    assert len(joined.message) <= errors.MAX_MESSAGE_CHARS


@SETTINGS
@given(data=st.data(), spec=st.sampled_from(BOUND), token=tokens,
       request_ids=st.tuples(st.uuids(), st.uuids()))
def test_bound_spec_404_equals_the_refusal(data, spec, token, request_ids):
    body_404 = data.draw(bodies(token))
    other = data.draw(st.one_of(st.just('uc-other'), st.none(), st.just(7)))
    refused_body = {}
    current = refused_body
    segments = spec.bind_usecase_path.split('.')
    for segment in segments[:-1]:
        current[segment] = {}
        current = current[segment]
    if other is not None:
        current[segments[-1]] = other
    args = {'usecase_id': 'uc-1'}
    missing = shaping.shape(spec, args, BackendResponse(404, body_404))
    refused = shaping.shape(spec, args, BackendResponse(
        data.draw(st.sampled_from((200, 201))), refused_body))
    assert refused.binding_refused and not missing.binding_refused
    first = missing.to_result(str(request_ids[0]))
    second = refused.to_result(str(request_ids[1]))
    assert first.is_error is second.is_error is True
    assert first.content == second.content
    strip = lambda result: {k: v for k, v in result.structured_content.items()
                            if k != 'request_id'}
    assert strip(first) == strip(second) == {
        'category': 'not_found', 'message': errors.BINDING_MESSAGE, 'status': 404}
    assert token not in json.dumps(first.model_dump(mode='json'))
