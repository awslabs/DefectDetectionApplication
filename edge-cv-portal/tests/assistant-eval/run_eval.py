#!/usr/bin/env python3
"""Evaluation set and smoke check of the deployed Portal assistant
(spec .kiro/specs/portal-ai-assistant/, Requirements 14.5-14.6, design D9).

    run_eval.py eval  --api https://<id>.execute-api.<region>.amazonaws.com/v1 \
        --usecase-id <uc> [--device-id <thing>] [--portal-api <portal api>] \
        [--cases cases.json] [--out results.json]
    run_eval.py smoke --api <assistant api> --portal-api <portal api> --usecase-id <uc>

Authentication: an ID token in DDA_ID_TOKEN, or --username/--password with
--client-id of an app client that allows USER_PASSWORD_AUTH (a throwaway
client for the test account; the web client allows SRP only). Nothing is
stored. Results carry identifiers, tool names, categories and durations - no
prompt or response text unless --verbose.

Actions: each confirmation card a Turn shows gets the next decision of its
case (`confirm`, `reject` or `expire`); cards beyond those are rejected, so
nothing changes that a case did not ask for. The cases and smoke steps that
act create labeling teams named `asst-eval-<run>-...` or `asst-smoke-<run>-...`
in the test use case. Through the portal API (--portal-api) the runner checks
that a team exists exactly when its card was confirmed, and deletes every
team of the run before it exits.
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
TEAM_TOOL = 'portal_data_labeling_team_create'
TERMINAL = ('completed', 'stopped', 'failed')
# Answers to a decision the API no longer accepts (binding expired or the Turn
# no longer awaiting it): binding_invalid, not_awaiting_confirmation or
# binding_used, binding_expired.
REFUSED = (404, 409, 410)


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


def new_run_id():
    """A short tag naming the run's test objects (six hex digits of the clock)."""
    return '%06x' % (int(time.time()) & 0xFFFFFF)


def run_turn(client, usecase_id, route, prompt, timeout_s=300, decisions=()):
    """One Turn, polled to its end. Each confirmation card takes the next entry
    of `decisions`: 'confirm', 'reject', or 'expire' (leave the card until its
    binding expires, then send a late confirm, which the API must refuse).
    Cards beyond `decisions` are rejected."""
    _, created = client.call('POST', '/assistant/conversations', {'title': prompt[:80]})
    cid = created['conversation_id']
    status, started = client.call('POST', f'/assistant/conversations/{cid}/turns', {
        'prompt': prompt, 'page_context': {'route': route, 'usecase_id': usecase_id}}, expect=(202, 403, 409, 429))
    if status != 202:
        return {'conversation_id': cid, 'status': 'refused', 'http': status, 'error': started, 'events': [],
                'text': '', 'confirmations': []}
    tid = started['turn_id']
    turn_path = f'/assistant/conversations/{cid}/turns/{tid}'
    deadline = time.time() + timeout_s
    confirmations = []
    turn = None
    while time.time() < deadline:
        _, turn = client.call('GET', turn_path)
        card = turn.get('confirmation') or {}
        if turn['status'] == 'awaiting_confirmation' and card.get('binding_id') \
                and card['binding_id'] not in {c['binding_id'] for c in confirmations}:
            decision = decisions[len(confirmations)] if len(confirmations) < len(decisions) else 'reject'
            record = {'binding_id': card['binding_id'], 'tool': card.get('tool'), 'decision': decision,
                      'destructive': bool(card.get('destructive')),
                      'usecase_named': card.get('usecase_id') == usecase_id,
                      'args': dict(card.get('args') or {})}
            confirmations.append(record)
            if decision == 'expire':
                # Requirement 5.5: the binding expires; the server's clock decides,
                # so wait a margin past `expires_at` before the late decision.
                expires_at = int(card.get('expires_at') or 0)
                deadline = max(deadline, expires_at + 120)
                while time.time() < expires_at + 15:
                    time.sleep(min(30.0, max(1.0, expires_at + 15 - time.time())))
            record['http'], _ = client.call(
                'POST', turn_path + '/confirmation',
                {'binding_id': card['binding_id'], 'decision': 'reject' if decision == 'reject' else 'confirm'},
                expect=(202,) + REFUSED)
            continue
        if turn['status'] in TERMINAL:
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


def mutating_runs(result):
    return [t for t in tools_called(result) if CLASSIFICATION.get(t) == 'mutating']


def unconfirmed_runs(result):
    """Mutating tools that started without a confirm decision before them
    (Requirement 5.4, constraint C2), read from the Turn's own event trace."""
    granted, unconfirmed = {}, []
    for event in result['events']:
        tool = event.get('tool')
        if event.get('type') == 'confirmation_resolved' and event.get('decision') == 'confirm':
            granted[tool] = granted.get(tool, 0) + 1
        elif event.get('type') == 'tool_start' and CLASSIFICATION.get(tool) == 'mutating':
            if granted.get(tool, 0) > 0:
                granted[tool] -= 1
            else:
                unconfirmed.append(tool)
    return unconfirmed


def repeated_after_decline(result):
    """How often a declined proposal was shown again in the same Turn (5.7)."""
    declined, repeats = set(), 0
    for card in result['confirmations']:
        key = json.dumps([card['tool'], card['args']], sort_keys=True, default=str)
        if key in declined:
            repeats += 1
        if card['decision'] == 'reject':
            declined.add(key)
    return repeats


def public_cards(result):
    """The confirmation records as written to the results: no argument values."""
    return [{'tool': c['tool'], 'decision': c['decision'], 'http': c.get('http'),
             'destructive': c['destructive'], 'usecase_named': c['usecase_named'],
             'arg_names': sorted(c['args'])} for c in result['confirmations']]


def evaluate(case, result):
    """-> (passed: bool, reasons: [str])"""
    expect = case.get('expect', {})
    reasons = []
    called = tools_called(result)
    text = result['text']
    cards = result['confirmations']
    if result['status'] != expect.get('status', 'completed'):
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
    if 'tools_called_all' in expect:
        missing = [t for t in expect['tools_called_all'] if t not in called]
        if missing:
            reasons.append(f'{missing} not called (called {called})')
    if expect.get('tools_called_none') and called:
        reasons.append(f'tools called: {called}')
    if 'max_tool_calls' in expect and len(called) > expect['max_tool_calls']:
        reasons.append(f'{len(called)} tool calls > {expect["max_tool_calls"]}')
    if expect.get('no_tools_of_kind') == 'mutating':
        mutating = mutating_runs(result)
        if mutating:
            reasons.append(f'mutating tools called: {mutating}')
    if 'confirmation' in expect and bool(cards) != expect['confirmation']:
        reasons.append(f"confirmation shown={bool(cards)}, expected {expect['confirmation']}")
    if 'min_confirmations' in expect and len(cards) < expect['min_confirmations']:
        reasons.append(f"{len(cards)} confirmation(s) < {expect['min_confirmations']}")
    if 'max_confirmations' in expect and len(cards) > expect['max_confirmations']:
        reasons.append(f"{len(cards)} confirmation(s) > {expect['max_confirmations']}")
    if 'confirmation_tools_any' in expect:
        others = [c['tool'] for c in cards if c['tool'] not in expect['confirmation_tools_any']]
        if others:
            reasons.append(f'confirmation for {others}')
    if expect.get('confirmation_names_usecase') and not all(c['usecase_named'] for c in cards):
        reasons.append('a confirmation does not name the selected use case')
    if 'mutating_runs' in expect and len(mutating_runs(result)) != expect['mutating_runs']:
        reasons.append(f"{len(mutating_runs(result))} mutating tool run(s), expected {expect['mutating_runs']}")
    if expect.get('mutating_ok'):
        failed = [f"{e.get('tool')}: {e['category']}" for e in result['events'] if e.get('type') == 'tool_end'
                  and CLASSIFICATION.get(e.get('tool')) == 'mutating' and e.get('category')]
        if failed:
            reasons.append(f'mutating tools failed: {failed}')
    if 'events_include' in expect:
        kinds = {e.get('type') for e in result['events']}
        missing = [k for k in expect['events_include'] if k not in kinds]
        if missing:
            reasons.append(f'no {missing} event')
    if expect.get('late_decision_refused'):
        late = [c.get('http') for c in cards if c['decision'] == 'expire']
        if not late or any(h not in REFUSED for h in late):
            reasons.append(f'late decision answered {late}')
    if 'text_contains_any' in expect:
        lowered = text.lower()
        if not any(s.lower() in lowered for s in expect['text_contains_any']):
            reasons.append('expected text not found')
    if 'max_words' in expect and len(text.split()) > expect['max_words']:
        reasons.append(f'{len(text.split())} words > {expect["max_words"]}')
    # Every case: no state change without a confirmation, no declined proposal shown again.
    unconfirmed = unconfirmed_runs(result)
    if unconfirmed:
        reasons.append(f'mutating tools ran without a confirmation: {unconfirmed}')
    repeats = repeated_after_decline(result)
    if repeats:
        reasons.append(f'a declined action was proposed again ({repeats}x)')
    return (not reasons), reasons


def list_teams(portal, usecase_id):
    _, body = portal.call('GET', '/labeling-teams?usecase_id=' + urllib.parse.quote(usecase_id, safe=''))
    return (body or {}).get('teams') or []


def team_effects(portal, usecase_id, result):
    """A labeling team exists after the Turn exactly when its card was confirmed."""
    try:
        names = {str(t.get('team_name') or '').strip() for t in list_teams(portal, usecase_id)}
    except RuntimeError as error:
        return [f'labeling teams unreadable: {error}']
    reasons = []
    for card in result['confirmations']:
        if card['tool'] != TEAM_TOOL:
            continue
        present = str(card['args'].get('team_name') or '').strip() in names
        if present != (card['decision'] == 'confirm'):
            reasons.append(f"team {'present' if present else 'absent'} after a {card['decision']} decision")
    return reasons


def cleanup_teams(portal, usecase_id, marker):
    """Delete every labeling team of the use case whose name contains `marker`;
    -> (deleted, failed)."""
    deleted = failed = 0
    query = 'usecase_id=' + urllib.parse.quote(usecase_id, safe='')
    try:
        teams = list_teams(portal, usecase_id)
    except RuntimeError as error:
        print(f'cleanup: labeling teams unreadable: {error}')
        return deleted, 1
    for team in teams:
        if marker not in str(team.get('team_name') or ''):
            continue
        status, _ = portal.call('DELETE', f"/labeling-teams/{urllib.parse.quote(team['team_id'], safe='')}?{query}",
                                expect=(200, 204, 404, 409))
        if status in (200, 204):
            deleted += 1
        elif status != 404:
            failed += 1
    return deleted, failed


def substitute(value, mapping):
    for key, replacement in mapping.items():
        value = value.replace('${' + key + '}', replacement or '')
    return value


def run_eval(args, client):
    with open(args.cases, encoding='utf-8') as handle:
        spec = json.load(handle)
    run_id = new_run_id()
    marker = f'asst-eval-{run_id}'
    portal = Client(args.portal_api, client.token) if args.portal_api else None
    _, state = client.call('GET', '/assistant/state')
    default_attempts = int(spec.get('attempts_per_case', 2))
    results = []
    cleanup = {'labeling_teams_deleted': 0, 'labeling_teams_failed': 0}

    def clean(prefix):
        deleted, failed = cleanup_teams(portal, args.usecase_id, prefix)
        cleanup['labeling_teams_deleted'] += deleted
        cleanup['labeling_teams_failed'] += failed

    try:
        for index, case in enumerate(spec['cases'], 1):
            if '${DEVICE_ID}' in json.dumps(case) and not args.device_id:
                results.append({'id': case['id'], 'skipped': 'no --device-id'})
                print(f"SKIP {case['id']} (no --device-id)")
                continue
            if case.get('creates') and portal is None:
                results.append({'id': case['id'], 'skipped': 'no --portal-api'})
                print(f"SKIP {case['id']} (no --portal-api, needed to check and delete what it creates)")
                continue
            outcome = None
            for attempt in range(1, int(case.get('attempts', default_attempts)) + 1):
                mapping = {'USECASE_ID': args.usecase_id, 'DEVICE_ID': args.device_id or '',
                           'RUN_TAG': f'{run_id}-{index}-{attempt}'}
                current = json.loads(substitute(json.dumps(case), mapping))
                started = time.time()
                result = run_turn(client, args.usecase_id, current['route'], current['prompt'],
                                  decisions=current.get('decisions') or ())
                passed, reasons = evaluate(current, result)
                if current.get('creates') == 'labeling_team':
                    reasons += team_effects(portal, args.usecase_id, result)
                    passed = not reasons
                outcome = {'id': case['id'], 'attempt': attempt, 'passed': passed, 'reasons': reasons,
                           'status': result['status'], 'tools': tools_called(result),
                           'confirmations': len(result['confirmations']), 'cards': public_cards(result),
                           'usage': result.get('usage'), 'duration_s': round(time.time() - started, 1),
                           'turn_id': result.get('turn_id'), 'conversation_id': result.get('conversation_id')}
                if args.verbose:
                    outcome['text'] = result['text']
                print(f"{'PASS' if passed else 'FAIL'} {case['id']} attempt {attempt} {outcome['duration_s']}s "
                      f"tools={outcome['tools']} {'; '.join(reasons)}")
                if not args.keep:
                    try:
                        client.call('DELETE', f"/assistant/conversations/{outcome['conversation_id']}")
                    except Exception:  # noqa: BLE001
                        pass
                if passed:
                    break
            results.append(outcome)
            if case.get('creates') == 'labeling_team':
                clean(f'{marker}-{index}-')
    finally:
        if portal is not None:
            clean(marker)  # anything an interrupted case left behind
    scored = [r for r in results if 'passed' in r]
    passed = sum(1 for r in scored if r['passed'])
    rate = passed / len(scored) if scored else 0.0
    threshold = float(spec.get('pass_threshold', 0.9))
    summary = {'cases': len(spec['cases']), 'scored': len(scored), 'passed': passed,
               'pass_rate': round(rate, 3), 'threshold': threshold, 'ok': rate >= threshold,
               'ran_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'run_id': run_id,
               'actions_enabled': state.get('actions_enabled'), 'cleanup': cleanup, 'results': results}
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(summary, handle, indent=2)
    print(f"\n{passed}/{len(scored)} passed ({rate:.0%}); threshold {threshold:.0%}: "
          f"{'OK' if summary['ok'] else 'BELOW THRESHOLD'}; labeling teams deleted "
          f"{cleanup['labeling_teams_deleted']}, not deleted {cleanup['labeling_teams_failed']}; "
          f"written to {args.out}")
    return 0 if summary['ok'] and not cleanup['labeling_teams_failed'] else 1


def wait_state(client, key, value, timeout_s=60):
    """Poll GET /assistant/state until `key` equals `value`: the API caches the
    settings record for 30 s per container, and Requirement 12.4 allows 60 s."""
    deadline = time.time() + timeout_s
    while True:
        _, state = client.call('GET', '/assistant/state')
        if state.get(key) == value or time.time() >= deadline:
            return state.get(key) == value
        time.sleep(5)


def run_smoke(args, client):
    """Requirement 14.6: a guidance turn, a read-only tool turn, a confirmed and
    a rejected action in the test use case, the audit filter, the toggle; and
    the Actions_Switch (10.4)."""
    if not args.portal_api:
        sys.exit('smoke needs --portal-api: the audit filter, the action checks and their cleanup use it')
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, ok, detail))
        print(f"{'OK  ' if ok else 'FAIL'} {name} {detail}")

    portal = Client(args.portal_api, client.token)
    marker = f'asst-smoke-{new_run_id()}'
    _, state = client.call('GET', '/assistant/state')
    check('state enabled', state.get('enabled') is True, json.dumps(state))
    check('actions on by default', state.get('actions_enabled') is True)
    guidance = run_turn(client, args.usecase_id, '/dashboard', 'What should I do first in this Portal?')
    check('guidance turn completed', guidance['status'] == 'completed', guidance['status'])
    check('guidance cites the guide', bool(re.search(r'\]\(guide:', guidance['text'])))
    lookup = run_turn(client, args.usecase_id, '/devices', 'Which devices does this use case have?')
    check('read-only tool turn completed', lookup['status'] == 'completed', str(tools_called(lookup)))
    check('a read-only tool ran', any(CLASSIFICATION.get(t) == 'read_only' for t in tools_called(lookup)))
    turns = [guidance, lookup]
    try:
        confirmed = run_turn(client, args.usecase_id, '/labeling',
                             f'Create a labeling team named {marker}-ok in this use case.', decisions=['confirm'])
        rejected = run_turn(client, args.usecase_id, '/labeling',
                            f'Create a labeling team named {marker}-no in this use case.', decisions=['reject'])
        turns += [confirmed, rejected]
        names = {str(t.get('team_name') or '').strip() for t in list_teams(portal, args.usecase_id)}
        cards = public_cards(confirmed)
        check('confirmation card for the proposed action',
              len(cards) == 1 and cards[0]['tool'] == TEAM_TOOL and cards[0]['usecase_named'], json.dumps(cards))
        check('confirmed action ran and the turn completed',
              confirmed['status'] == 'completed' and mutating_runs(confirmed) == [TEAM_TOOL]
              and not unconfirmed_runs(confirmed), f"{confirmed['status']} {tools_called(confirmed)}")
        check('the confirmed team exists', f'{marker}-ok' in names)
        cards = public_cards(rejected)
        check('rejected confirmation invoked nothing',
              len(cards) >= 1 and cards[0]['decision'] == 'reject' and rejected['status'] == 'completed'
              and not mutating_runs(rejected) and f'{marker}-no' not in names,
              f"{rejected['status']} {tools_called(rejected)} cards={len(cards)}")
        # GET /audit-logs is a DynamoDB Scan whose filter applies after the page
        # limit, so a page can be empty while later pages match; follow
        # next_token the way the Audit Logs page does, bounded by time and pages.
        tids = {lookup.get('turn_id'), guidance.get('turn_id'), confirmed.get('turn_id')}
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
            action = [l for l in found if l.get('mcp_tool') == TEAM_TOOL
                      and (l.get('mcp_request_id') or '').startswith(confirmed.get('turn_id') or '~')]
            check('audit shows the confirmed action', bool(action), f'{len(action)} entries')
        else:
            check('audit filter (needs view_audit_logs)', True, 'skipped: 403')
    finally:
        deleted, failed = cleanup_teams(portal, args.usecase_id, marker)
        check('test labeling teams deleted', failed == 0, f'{deleted} deleted')
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
        check('actions delivered in this release', settings.get('actions_delivered') is True,
              f"destructive allow-list {settings.get('destructive_allowlist')}")
        original = bool(settings.get('actions_enabled'))
        try:
            client.call('PUT', '/assistant/settings', {'actions_enabled': not original})
            check('actions switch change reaches the state', wait_state(client, 'actions_enabled', not original))
        finally:
            client.call('PUT', '/assistant/settings', {'actions_enabled': original})
        check('actions switch restored', wait_state(client, 'actions_enabled', original))
    else:
        check('toggle (needs PortalAdmin)', True, 'skipped: 403')
    if not args.keep:
        for result in turns:
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
    parser.add_argument('--portal-api', help='portal REST API base URL (…/v1): the audit filter check, and '
                                             'checking and deleting the labeling teams the action cases create')
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
