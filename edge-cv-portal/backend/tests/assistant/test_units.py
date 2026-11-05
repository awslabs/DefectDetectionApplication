"""Unit tests of the small assistant modules: schemas, limits, guide,
journey, tools, prompts, bedrock stream parsing, telemetry (design §4, §5;
tasks.md 2.1, 2.4-2.8). Requirements 3.1, 4.1-4.2, 5.2, 7.3, 7.6, 9.5, 10.6.
"""
import json
import logging

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from assistant import ACTIONS_DELIVERED, DESTRUCTIVE_ALLOWLIST, bedrock, guide, journey, limits, prompts, schemas, telemetry, tools
from assistant.errors import AssistantError
from mcp_portal.catalog import CATALOG

from conftest import client_error, text_events, tool_use_events


# ------------------------------------------------------------------ schemas

def test_turn_start_schema_accepts_a_minimal_body():
    body = {'prompt': 'hi', 'page_context': {'route': '/devices'}}
    assert schemas.validate(schemas.TURN_START, body) is body


@pytest.mark.parametrize('body, field', [
    ({'page_context': {'route': '/x'}}, 'prompt'),
    ({'prompt': '', 'page_context': {'route': '/x'}}, 'prompt'),
    ({'prompt': 'x' * 4001, 'page_context': {'route': '/x'}}, 'prompt'),
    ({'prompt': 'x', 'page_context': {'route': 'devices'}}, 'page_context.route'),
    ({'prompt': 'x', 'page_context': {'route': '/x', 'token': 'abc'}}, 'page_context.token'),
    ({'prompt': 'x', 'page_context': {'route': '/x'}, 'identity': 'me'}, 'identity'),
])
def test_turn_start_schema_names_the_failing_field(body, field):
    with pytest.raises(AssistantError) as info:
        schemas.validate(schemas.TURN_START, body)
    assert info.value.code == 'invalid_input' and info.value.details == {'field': field}


def test_whitespace_prompt_is_rejected():
    with pytest.raises(AssistantError):
        schemas.validate_prompt('   \n ')


def test_settings_schema_ranges():
    assert schemas.validate(schemas.SETTINGS_PUT, {'retention_days': 1})
    for bad in ({}, {'retention_days': 0}, {'retention_days': 366}, {'guardrail_id': 'UPPER'},
                {'guardrail_version': 'v1'}, {'enabled': 'yes'}):
        with pytest.raises(AssistantError):
            schemas.validate(schemas.SETTINGS_PUT, bad)


# ------------------------------------------------------------------- limits

def test_limits_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv(limits.ENV_VAR, raising=False)
    assert limits.limits() == limits.DEFAULTS
    monkeypatch.setenv(limits.ENV_VAR, json.dumps({'turns_per_hour': 5, 'bogus': 1,
                                                   'tokens_per_day': -1, 'turn_seconds': True}))
    values = limits.limits()
    assert values['turns_per_hour'] == 5
    assert values['tokens_per_day'] == limits.DEFAULTS['tokens_per_day']
    assert values['turn_seconds'] == limits.DEFAULTS['turn_seconds']
    monkeypatch.setenv(limits.ENV_VAR, 'not json')
    assert limits.limits() == limits.DEFAULTS


# -------------------------------------------------------------------- guide

def test_route_map_matches_the_shipped_chapters(assistant_env):
    shipped = set(guide.list_chapters())
    mapped = {r['chapter'] for r in guide.route_map()['routes']}
    non_route = set(guide.route_map()['non_route_chapters'])
    assert mapped <= shipped, mapped - shipped
    assert shipped == mapped | non_route, shipped ^ (mapped | non_route)


@pytest.mark.parametrize('route, chapter, domain', [
    ('/dashboard', '01-getting-started.md', 'admin'),
    ('/devices/thing-1', '15-devices.md', 'devices'),
    ('/labeling/datasets', '03-data-management.md', 'data'),
    ('/labeling/job-1/review', '04-labeling.md', 'data'),
    ('/node-designer/review', '17-plugin-review.md', 'workflows'),
    ('/node-designer/plugins/p1', '10-node-designer.md', 'workflows'),
    ('/workflows/builder/wf-1?x=1', '08-workflows.md', 'workflows'),
    ('/unknown', None, None),
])
def test_chapter_and_domain_for_route(route, chapter, domain):
    assert guide.chapter_for_route(route) == chapter
    assert guide.domain_for_route(route) == domain


def test_guide_reads_are_bounded_and_safe(assistant_env):
    index = guide.read_index()
    assert 'end-to-end path' in index.lower()
    text, truncated = guide.read_chapter('02-use-cases.md')
    assert text and len(text) <= guide.MAX_CHARS + 40
    assert guide.read_chapter('../secrets.md') is None
    assert guide.read_chapter('02-use-cases.md/../x') is None
    assert not guide.valid_name('README.md/../x')
    section = guide.read_section('02-use-cases.md', 'Use Cases')
    assert section is not None and section[1].startswith('guide:02-use-cases.md#')
    assert guide.read_section('02-use-cases.md', 'no such heading zzz') is None


# ------------------------------------------------------------------ journey

def test_next_step_walks_the_path():
    assert journey.next_step({})['number'] == 2
    some = {k: journey.SOME for k in journey.PROBE_KEYS}
    assert journey.next_step(some)['done'] is True
    partial = dict(some, devices=journey.NONE)
    assert journey.next_step(partial)['number'] == 7
    unknown = dict(some, workflows=journey.NO_PERMISSION)
    step = journey.next_step(unknown)
    assert step['number'] == 5 and step['unknown'] == ['workflows']


@given(st.fixed_dictionaries({k: st.sampled_from([journey.SOME, journey.NONE, journey.NO_PERMISSION,
                                                   journey.ERROR]) for k in journey.PROBE_KEYS}))
@hsettings(max_examples=60)
def test_property_next_step_is_the_first_unmet_step(summary):
    step = journey.next_step(summary)
    for earlier in journey.JOURNEY_STEPS:
        if earlier['number'] >= step['number']:
            break
        assert all(summary.get(k) == journey.SOME for k in earlier['keys'])
    if not step['done']:
        unmet = [k for k in journey.step_by_number(step['number']) and
                 next(s for s in journey.JOURNEY_STEPS if s['number'] == step['number'])['keys']
                 if summary.get(k) != journey.SOME]
        assert unmet
        assert step['unknown'] == [k for k in unmet if summary.get(k) in journey.UNKNOWN_VALUES]


def test_summary_from_outcomes_reads_counts_and_lists():
    class Outcome:
        def __init__(self, result=None, failure=None):
            self.failure = failure
            self.shaped = None if failure else type('S', (), {'result': result})()

    class Failure:
        category = 'not_authorized'

    outcomes = {k: Outcome({'items': []}) for k in journey.PROBE_KEYS}
    outcomes['datasets'] = Outcome({'count': 3})
    outcomes['devices'] = Outcome({'devices': [{'device_id': 'd'}]})
    outcomes['workflows'] = Outcome(failure=Failure())
    outcomes['models'] = Outcome({'models': [{'model_id': 'm'}]})
    summary = journey.summary_from_outcomes(outcomes)
    assert summary['datasets'] == journey.SOME and summary['devices'] == journey.SOME
    assert summary['workflows'] == journey.NO_PERMISSION
    assert summary['trained_models'] == journey.SOME  # an imported model counts
    assert summary['labeling_jobs'] == journey.NONE


# -------------------------------------------------------------------- tools

def test_property_bedrock_tool_specs_match_the_catalog():
    for spec in CATALOG:
        rendered = tools.bedrock_tool_spec(spec)['toolSpec']
        assert rendered['name'] == spec.name
        assert rendered['description'] == tools.render_description(spec)
        adapted = rendered['inputSchema']['json']
        assert adapted['type'] == 'object'
        assert not any(k in adapted for k in ('oneOf', 'anyOf', 'allOf'))
        original = {k: v for k, v in spec.input_schema.items() if k not in ('oneOf', 'anyOf', 'allOf',
                                                                             'description')}
        assert {k: v for k, v in adapted.items() if k != 'description'} == {**original, 'type': 'object'}
        import re
        assert re.match(r'^[a-zA-Z0-9_-]{1,64}$', rendered['name'])


def test_phase_2_delivers_actions_with_the_signed_off_allowlist():
    """Requirement 15.2: actions delivered; every allow-listed name is a
    destructive tool of the delivered catalog; nothing destructive outside the
    list is ever exposed, with the switch on or off."""
    assert ACTIONS_DELIVERED is True
    destructive = {s.name for s in tools.delivered_specs() if s.destructive}
    assert set(DESTRUCTIVE_ALLOWLIST) <= destructive, set(DESTRUCTIVE_ALLOWLIST) - destructive
    assert len(set(DESTRUCTIVE_ALLOWLIST)) == len(DESTRUCTIVE_ALLOWLIST)
    for domain in ('admin', 'data', 'devices', 'models', 'workflows'):
        exposed = {s.name for s in tools.exposed_specs(domain, True, set())}
        offered_destructive = exposed & destructive
        assert offered_destructive <= set(DESTRUCTIVE_ALLOWLIST), offered_destructive
        # the page's non-destructive mutating tools are offered with the switch on
        domain_mutating = {s.name for s in tools.delivered_specs()
                           if s.domain == domain and not s.read_only and not s.destructive}
        assert domain_mutating <= exposed
        # and nothing mutating with the switch off
        assert all(s.read_only for s in tools.exposed_specs(domain, False, set()))


def test_exposure_is_read_only_while_actions_are_undelivered(monkeypatch):
    monkeypatch.setattr(tools, 'ACTIONS_DELIVERED', False)
    specs = tools.exposed_specs('devices', True, {'portal_devices_deployment_create'})
    assert all(s.read_only for s in specs)
    assert len(specs) == sum(1 for s in tools.delivered_specs() if s.read_only)
    assert tools.mutating_allowed(tools.BY_NAME['portal_devices_deployment_create'], True) is False


def test_exposure_with_actions_delivered(monkeypatch):
    monkeypatch.setattr(tools, 'ACTIONS_DELIVERED', True)
    monkeypatch.setattr(tools, 'DESTRUCTIVE_ALLOWLIST', ('portal_devices_deployment_cancel',))
    specs = tools.exposed_specs('devices', True, {'portal_workflows_create'})
    names = {s.name for s in specs}
    assert 'portal_devices_flags_update' in names            # devices, non-destructive
    assert 'portal_devices_deployment_cancel' in names       # allow-listed destructive
    assert 'portal_devices_delete' not in names              # destructive, not allow-listed
    assert 'portal_workflows_create' in names                # extra from find_tools
    assert 'portal_workflows_update' not in names            # other domain, not extra
    off = tools.exposed_specs('devices', False, set())
    assert all(s.read_only for s in off)


def test_find_tools_and_result_blocks():
    found = tools.find_tools('device logs', tools.allowed_specs(False))
    assert found and all(t['classification'] == 'read-only' for t in found)
    assert found[0]['name'].startswith('portal_devices_logs')
    block = tools.tool_result_block('tu-1', {'a': 1})
    assert block['toolResult'] == {'toolUseId': 'tu-1', 'content': [{'json': {'a': 1}}],
                                   'status': 'success'}
    huge = tools.tool_result_block('tu-2', {'x': 'y' * 40_000}, True)
    assert 'text' in huge['toolResult']['content'][0] and huge['toolResult']['status'] == 'error'
    assert len(tools.local_tool_specs(['01-getting-started.md'])) == 3


def test_run_local_tools(assistant_env):
    payload = tools.run_local_tool('guide_read_section', {'chapter': '15-devices.md'},
                                   call=lambda *_: None, allowed=[])
    assert payload['link'] == 'guide:15-devices.md' and payload['text']
    with pytest.raises(tools.LocalToolError) as info:
        tools.run_local_tool('guide_read_section', {'chapter': '../x.md'}, call=lambda *_: None,
                             allowed=[])
    assert info.value.category == 'invalid_input'
    with pytest.raises(tools.LocalToolError):
        tools.run_local_tool('journey_status', {}, call=lambda *_: None, allowed=[])
    result = tools.run_local_tool('find_tools', {'query': 'audit'}, call=lambda *_: None,
                                  allowed=tools.allowed_specs(False))
    assert any(t['name'] == 'portal_admin_audit_logs_query' for t in result['tools'])


# ------------------------------------------------------------------ prompts

def test_system_prompt_structure():
    blocks = prompts.build_system(index_text='INDEX', chapter_text='CHAPTER', chapter_name='15-devices.md',
                                  page_context={'route': '/devices', 'params': {'deviceId': 'd-1'},
                                                'usecase_id': 'uc-1'},
                                  principal_display='a@example.com', claimed_role='Viewer',
                                  actions_enabled=False, now_iso='2026-11-03T00:00Z',
                                  support_email='help@example.com', cache_points=True)
    assert 'cachePoint' in blocks[1]
    assert blocks[0]['text'].endswith('INDEX')
    assert 'never add a `confirm`'.lower() in blocks[0]['text'].lower()
    assert 'CHAPTER' in blocks[2]['text']
    dynamic = blocks[3]['text']
    assert '/devices' in dynamic and 'deviceId=d-1' in dynamic and 'uc-1' in dynamic
    assert 'disabled on this deployment' in dynamic and 'help@example.com' in dynamic
    without = prompts.build_system(index_text='I', chapter_text=None, chapter_name=None,
                                   page_context={'route': '/'}, principal_display='u',
                                   claimed_role='Viewer', actions_enabled=True, now_iso='t',
                                   cache_points=False)
    assert len(without) == 2 and 'none selected' in without[1]['text']


# ------------------------------------------------------------------ bedrock

def test_stream_parser_collects_text_tool_uses_and_usage():
    events = tool_use_events([('tu-1', 'portal_devices_list', {'usecase_id': 'uc-1'}),
                              ('tu-2', 'guide_read_section', {'chapter': '15-devices.md'})],
                             text='Let me check.')
    seen = []
    response = bedrock.stream(type('C', (), {'converse_stream': staticmethod(
        lambda **_: {'stream': iter(events)})})(), {'modelId': 'm'}, seen.append)
    assert response.text == 'Let me check.' and ''.join(seen) == 'Let me check.'
    assert [u['name'] for u in response.tool_uses] == ['portal_devices_list', 'guide_read_section']
    assert response.tool_uses[0]['input'] == {'usecase_id': 'uc-1'}
    assert response.content[0] == {'text': 'Let me check.'} and 'toolUse' in response.content[1]
    assert response.stop_reason == 'tool_use' and response.usage['input'] == 120
    plain = bedrock.stream(type('C', (), {'converse_stream': staticmethod(
        lambda **_: {'stream': iter(text_events('Hello world'))})})(), {'modelId': 'm'})
    assert plain.text == 'Hello world' and plain.tool_uses == [] and plain.stop_reason == 'end_turn'


def test_stream_errors_are_classified():
    def failing(error):
        return type('C', (), {'converse_stream': staticmethod(lambda **_: (_ for _ in ()).throw(error))})()

    with pytest.raises(bedrock.ModelError) as info:
        bedrock.stream(failing(client_error('ThrottlingException')), {})
    assert info.value.code == 'bedrock_throttled' and info.value.retryable
    with pytest.raises(bedrock.ModelError) as info:
        bedrock.stream(failing(client_error('AccessDeniedException')), {})
    assert info.value.code == 'bedrock_authorization'
    with pytest.raises(bedrock.ToolUseUnsupported):
        bedrock.stream(failing(client_error('ValidationException', 'toolConfig is not supported')), {})
    with pytest.raises(bedrock.CachePointUnsupported):
        bedrock.stream(failing(client_error('ValidationException', 'cachePoint not supported')), {})
    with pytest.raises(bedrock.ModelError) as info:
        bedrock.stream(failing(client_error('ValidationException', 'maxTokens too large')), {})
    assert info.value.code == 'bedrock_model_access'


def test_cache_point_support_and_request_assembly(monkeypatch):
    assert bedrock.supports_cache_points('us.anthropic.claude-sonnet-4-5-20250929-v1:0')
    assert bedrock.supports_cache_points('anthropic.claude-3-7-sonnet-20250219-v1:0')
    # a global inference profile and a generation newer than the 4.x names
    assert bedrock.supports_cache_points('global.anthropic.claude-fable-5')
    assert bedrock.supports_cache_points('apac.amazon.nova-pro-v1:0')
    assert not bedrock.supports_cache_points('meta.llama3-1-70b-instruct-v1:0')
    assert not bedrock.supports_cache_points('us.meta.llama3-1-70b-instruct-v1:0')
    assert not bedrock.supports_cache_points('')
    monkeypatch.setenv(bedrock.CACHE_PREFIXES_ENV, 'meta.llama')
    assert bedrock.supports_cache_points('us.meta.llama3-1-70b-instruct-v1:0')
    assert not bedrock.supports_cache_points('anthropic.claude-3-7-sonnet-20250219-v1:0')
    request = bedrock.build_request(
        model_id='m', system=[{'text': 's'}, {'cachePoint': {'type': 'default'}}],
        messages=[{'role': 'user', 'content': [{'text': 'hi'}]}],
        tool_config={'tools': [{'toolSpec': {'name': 't'}}, {'cachePoint': {'type': 'default'}}]},
        inference_config={'maxTokens': 10}, guardrail={'guardrail_id': 'abc', 'guardrail_version': None})
    assert request['guardrailConfig'] == {'guardrailIdentifier': 'abc', 'guardrailVersion': 'DRAFT'}
    stripped = bedrock.strip_cache_points(request)
    assert stripped['system'] == [{'text': 's'}] and stripped['toolConfig']['tools'] == [{'toolSpec': {'name': 't'}}]


# ---------------------------------------------------------------- telemetry

def test_telemetry_records_have_fixed_keys(caplog, capsys):
    with caplog.at_level(logging.INFO, logger='assistant.telemetry'):
        record = telemetry.log_turn(sub='s', conversation_id='c', turn_id='t', status='completed',
                                    failure_code=None, model_calls=1, tool_calls=2, pipeline_calls=3,
                                    usage={'input': 10, 'output': 5}, duration_ms=12, text_chars=40,
                                    confirmations=0, client='dda-portal-frontend/abc', version='1.0.0')
    assert tuple(record) == telemetry.TURN_FIELDS
    assert json.loads(caplog.records[-1].getMessage())['turn_id'] == 't'
    emf = telemetry.metrics({'Turns': 1}, {'status': 'completed'})
    out = capsys.readouterr().out
    assert '"Namespace":"DDA/Assistant"' in out and emf['Turns'] == 1.0
    assert telemetry.metric_names([emf]) == {'Turns'}
