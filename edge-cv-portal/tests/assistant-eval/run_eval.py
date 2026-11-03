#!/usr/bin/env python3
"""Evaluation set and smoke check of the deployed Portal assistant
(spec .kiro/specs/portal-ai-assistant/, Requirements 14.5-14.6, design D9).

    run_eval.py eval  --api https://<id>.execute-api.<region>.amazonaws.com/v1 \
        --usecase-id <uc> [--device-id <thing>] [--cases cases.json] [--out results.json]
    run_eval.py smoke --api <assistant api> --portal-api <portal api> --usecase-id <uc>

Authentication: an ID token in DDA_ID_TOKEN, or --username/--password with
--client-id of an app client that allows USER_PASSWORD_AUTH (a throwaway
client for the test account; the web client allows SRP only). Nothing is
stored. Results carry identifiers, tool names, categories and durations - no
prompt or response text unless --verbose.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MUTATING_PREFIX_HINT = None  # filled from the catalog file when available


def _catalog_classification():
    """tool name -> 'read_only' | 'mutating' from the repo's catalog export."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, '..', '..', 'backend', 'functions', 'mcp_portal', 'catalog_tools.json')
    try:
        with open(path, encoding='utf-8') as handle:
            return {t['name']: t['classification'] for t in json.load(handle)}
    except OSError:
        return {}


CLASSIFICATION = _catalog_classification()
LOCAL_TOOLS = {'guide_read_section', 'journey_status', 'find_tools'}


class Client:
    def __init__(self, api, token, build='eval'):
        self.api = api.rstrip('/')
        self.token = token
        self.build = build

    def call(self, method, path, body=None, expect=(200, 201, 202, 204)):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(self.api + path, data=data, method=method, headers={
            'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json',
            'X-Portal-Build': self.build})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = {'raw': raw.decode('utf-8', 'replace')[:200]}
            if error.code in expect:
                return error.code, payload
            raise RuntimeError(f'{method} {path} -> {error.code} {payload}') from error


def sign_in(args):
    token = os.environ.get('DDA_ID_TOKEN')
    if token:
        return token
    if not (args.username and args.password and args.client_id):
        sys.exit('set DDA_ID_TOKEN, or pass --username, --password and --client-id')
    import boto3
    cognito = boto3.client('cognito-idp', region_name=args.region)
    response = cognito.initiate_auth(
        AuthFlow='USER_PASSWORD_AUTH', ClientId=args.client_id,
        AuthParameters={'USERNAME': args.username, 'PASSWORD': args.password})
    return response['AuthenticationResult']['IdToken']


def run_turn(client, usecase_id, route, prompt, timeout_s=300, reject_confirmations=True):
    _, created = client.call('POST', '/assistant/conversations', {'title': prompt[:80]})
    cid = created['conversation_id']
    status, started = client.call('POST', f'/assistant/conversations/{cid}/turns', {
        'prompt': prompt, 'page_context': {'route': route, 'usecase_id': usecase_id}}, expect=(202, 403, 409, 429))
    if status != 202:
        return {'conversation_id': cid, 'status': 'refused', 'http': status, 'error': started, 'events': [],
                'text': '', 'confirmations': 0}
    tid = started['turn_id']
    deadline = time.time() + timeout_s
    confirmations = 0
    turn = None
    while time.time() < deadline:
        _, turn = client.call('GET', f'/assistant/conversations/{cid}/turns/{tid}')
        if turn['status'] == 'awaiting_confirmation':
            confirmations += 1
            if reject_confirmations:
                client.call('POST', f'/assistant/conversations/{cid}/turns/{tid}/confirmation',
                            {'binding_id': turn['confirmation']['binding_id'], 'decision': 'reject'})
        if turn['status'] in ('completed', 'stopped', 'failed'):
            break
        time.sleep(1.0)
    turn = turn or {}
    return {'conversation_id': cid, 'turn_id': tid, 'status': turn.get('status', 'timeout'),
            'events': turn.get('events') or [], 'text': turn.get('text') or '',
            'failure': turn.get('failure'), 'confirmations': confirmations, 'usage': turn.get('usage')}


def tools_called(result):
    return [e.get('tool') for e in result['events'] if e.get('type') == 'tool_start' and e.get('tool')]


def denied_tools(result):
    return [e.get('tool') for e in result['events'] if e.get('type') == 'tool_end'
            and e.get('category') == 'not_authorized']


def evaluate(case, result):
    """-> (passed: bool, reasons: [str])"""
    expect = case.get('expect', {})
    reasons = []
    called = tools_called(result)
    text = result['text']
    if result['status'] != 'completed':
        reasons.append(f"status {result['status']} ({(result.get('failure') or {}).get('code')})")
    if expect.get('citation') and not re.search(r'\]\(guide:[0-9A-Za-z-]+\.md', text):
        reasons.append('no guide citation')
    if 'tools_called_any' in expect:
        wanted = expect['tools_called_any']
        if not any(t in called for t in wanted):
            if expect.get('accept_not_authorized') and denied_tools(result):
                pass
            else:
                reasons.append(f'none of {wanted} called (called {called})')
    if expect.get('tools_called_none') and called:
        reasons.append(f'tools called: {called}')
    if 'max_tool_calls' in expect and len(called) > expect['max_tool_calls']:
        reasons.append(f'{len(called)} tool calls > {expect["max_tool_calls"]}')
    if expect.get('no_tools_of_kind') == 'mutating':
        mutating = [t for t in called if CLASSIFICATION.get(t) == 'mutating']
        if mutating:
            reasons.append(f'mutating tools called: {mutating}')
    if 'confirmation' in expect and bool(result['confirmations']) != expect['confirmation']:
        reasons.append(f"confirmation shown={bool(result['confirmations'])}, expected {expect['confirmation']}")
    if 'text_contains_any' in expect:
        lowered = text.lower()
        if not any(s.lower() in lowered for s in expect['text_contains_any']):
            reasons.append('expected text not found')
    if 'max_words' in expect and len(text.split()) > expect['max_words']:
        reasons.append(f'{len(text.split())} words > {expect["max_words"]}')
    return (not reasons), reasons


def substitute(value, mapping):
    for key, replacement in mapping.items():
        value = value.replace('${' + key + '}', replacement or '')
    return value


def run_eval(args, client):
    with open(args.cases, encoding='utf-8') as handle:
        spec = json.load(handle)
    mapping = {'USECASE_ID': args.usecase_id, 'DEVICE_ID': args.device_id or ''}
    attempts = int(spec.get('attempts_per_case', 2))
    results = []
    for case in spec['cases']:
        if '${DEVICE_ID}' in json.dumps(case) and not args.device_id:
            results.append({'id': case['id'], 'skipped': 'no --device-id'})
            print(f"SKIP {case['id']} (no --device-id)")
            continue
        outcome = None
        for attempt in range(1, attempts + 1):
            started = time.time()
            result = run_turn(client, args.usecase_id, substitute(case['route'], mapping),
                              substitute(case['prompt'], mapping))
            passed, reasons = evaluate(case, result)
            outcome = {'id': case['id'], 'attempt': attempt, 'passed': passed, 'reasons': reasons,
                       'status': result['status'], 'tools': tools_called(result),
                       'confirmations': result['confirmations'], 'usage': result.get('usage'),
                       'duration_s': round(time.time() - started, 1),
                       'turn_id': result.get('turn_id'), 'conversation_id': result.get('conversation_id')}
            if args.verbose:
                outcome['text'] = result['text']
            print(f"{'PASS' if passed else 'FAIL'} {case['id']} attempt {attempt} {outcome['duration_s']}s "
                  f"tools={outcome['tools']} {'; '.join(reasons)}")
            if passed:
                break
        results.append(outcome)
        if not args.keep:
            try:
                client.call('DELETE', f"/assistant/conversations/{outcome['conversation_id']}")
            except Exception:  # noqa: BLE001
                pass
    scored = [r for r in results if 'passed' in r]
    passed = sum(1 for r in scored if r['passed'])
    rate = passed / len(scored) if scored else 0.0
    threshold = float(spec.get('pass_threshold', 0.9))
    summary = {'cases': len(spec['cases']), 'scored': len(scored), 'passed': passed,
               'pass_rate': round(rate, 3), 'threshold': threshold, 'ok': rate >= threshold,
               'ran_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'results': results}
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(summary, handle, indent=2)
    print(f"\n{passed}/{len(scored)} passed ({rate:.0%}); threshold {threshold:.0%}: "
          f"{'OK' if summary['ok'] else 'BELOW THRESHOLD'}; written to {args.out}")
    return 0 if summary['ok'] else 1


def run_smoke(args, client):
    """Requirement 14.6: a guidance turn, a read-only tool turn, a rejected
    confirmation (only once actions exist), the audit filter, the toggle."""
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, ok, detail))
        print(f"{'OK  ' if ok else 'FAIL'} {name} {detail}")

    _, state = client.call('GET', '/assistant/state')
    check('state enabled', state.get('enabled') is True, json.dumps(state))
    guidance = run_turn(client, args.usecase_id, '/dashboard', 'What should I do first in this Portal?')
    check('guidance turn completed', guidance['status'] == 'completed', guidance['status'])
    check('guidance cites the guide', bool(re.search(r'\]\(guide:', guidance['text'])))
    lookup = run_turn(client, args.usecase_id, '/devices', 'Which devices does this use case have?')
    check('read-only tool turn completed', lookup['status'] == 'completed', str(tools_called(lookup)))
    check('a read-only tool ran', any(CLASSIFICATION.get(t) == 'read_only' for t in tools_called(lookup)))
    if args.portal_api:
        portal = Client(args.portal_api, client.token)
        # GET /audit-logs is a DynamoDB Scan whose filter applies after the page
        # limit, so a page can be empty while later pages match; follow
        # next_token the way the Audit Logs page does, bounded by time and pages.
        tids = {lookup.get('turn_id'), guidance.get('turn_id')}
        since = int((time.time() - 1800) * 1000)
        path = f'/audit-logs?channel=assistant&limit=500&start_time={since}'
        found, status, pages = [], None, 0
        while path and pages < 60:
            status, logs = portal.call('GET', path, expect=(200, 403))
            if status != 200:
                break
            pages += 1
            found += [l for l in logs.get('logs', []) if l.get('channel') == 'assistant'
                      and any((l.get('mcp_request_id') or '').startswith(t or '~') for t in tids)]
            token = logs.get('next_token')
            path = f'/audit-logs?channel=assistant&limit=500&start_time={since}&next_token={urllib.parse.quote(token)}' if token else None
        if status == 200:
            tools_seen = sorted({l.get('mcp_tool') for l in found if l.get('mcp_tool')})
            check('audit filter shows the assistant entries', bool(found),
                  f'{len(found)} entries over {pages} page(s); tools {tools_seen}')
        else:
            check('audit filter (needs view_audit_logs)', True, 'skipped: 403')
    # the toggle (PortalAdmin): off -> state false -> on
    status, settings = client.call('GET', '/assistant/settings', expect=(200, 403))
    if status == 200:
        client.call('PUT', '/assistant/settings', {'enabled': False})
        _, off = client.call('GET', '/assistant/state')
        check('toggle off hides the assistant', off.get('enabled') is False and off.get('reason') == 'disabled_by_admin')
        refused_status, _ = client.call('POST', '/assistant/conversations', {}, expect=(201, 403))
        check('turns refused while off', refused_status == 403)
        client.call('PUT', '/assistant/settings', {'enabled': True})
        _, on = client.call('GET', '/assistant/state')
        check('toggle on restores the assistant', on.get('enabled') is True)
        check('actions not delivered in this release', settings.get('actions_delivered') is False)
    else:
        check('toggle (needs PortalAdmin)', True, 'skipped: 403')
    if not args.keep:
        for result in (guidance, lookup):
            try:
                client.call('DELETE', f"/assistant/conversations/{result['conversation_id']}")
            except Exception:  # noqa: BLE001
                pass
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 0 if not failed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['eval', 'smoke'])
    parser.add_argument('--api', required=True, help='assistant API base URL (…/v1)')
    parser.add_argument('--portal-api', help='portal REST API base URL (…/v1), for the audit filter check')
    parser.add_argument('--usecase-id', required=True)
    parser.add_argument('--device-id')
    parser.add_argument('--cases', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cases.json'))
    parser.add_argument('--out', default='assistant-eval-results.json')
    parser.add_argument('--region', default=os.environ.get('AWS_REGION', 'us-east-1'))
    parser.add_argument('--username')
    parser.add_argument('--password')
    parser.add_argument('--client-id')
    parser.add_argument('--keep', action='store_true', help='keep the conversations')
    parser.add_argument('--verbose', action='store_true', help='record response text in the results')
    args = parser.parse_args()
    client = Client(args.api, sign_in(args))
    return run_eval(args, client) if args.mode == 'eval' else run_smoke(args, client)


if __name__ == '__main__':
    sys.exit(main())
