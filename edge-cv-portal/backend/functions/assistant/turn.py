"""The agent loop of one Turn (design §8; Requirements 5.3-5.9, 6.8, 7.5,
8.2-8.3, 10.1, 11.2-11.3).

`run(cid, tid, resume=None, remaining_ms=...)` loads the Turn, streams the
model, runs read-only tools inline through `mcp_portal.pipeline.call_tool`,
suspends the Turn when the model proposes a state change (status
`awaiting_confirmation`) and resumes it from the consumed binding, persists
progress into the Turn item as it goes, and finishes with `completed`,
`stopped` or `failed`. Every exit path goes through `_finish`.
"""
import datetime as dt
import json
import logging
import math
import os
import time
from typing import Any, Callable, Dict, List, Optional

import bedrock_common

from assistant import SERVER_NAME, __version__
from assistant import bedrock, guide, limits as limits_module, prompts, settings, store, telemetry, tools
from mcp_portal import pipeline
from mcp_portal.dispatch import McpContext
from mcp_portal.principal import principal_from_claims

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 0.4
THROTTLE_BACKOFF_S = (1.0, 3.0)
MIN_REMAINING_MS = 20_000
MAX_TEXT_CHARS = 60_000
STORED_RESULT_CHARS = 8_000
IDENTIFIER_KEYS = ('usecase_id', 'name', 'status', 'limit')
# Weight of a cache-read token in the daily quota: Bedrock bills a cache read
# at one tenth of the input price, and the quota is a cost cap (COST_ESTIMATE:
# "1.5M tokens caps one user at about $5/day"). Counted at full weight, the
# ~27k cached tokens every model call reads exhausted a day in ~29 Turns on
# the first deployed evaluation run (2026-11-03).
CACHE_READ_WEIGHT = 0.1


class TurnEnded(Exception):
    """Raised by `_finish` to unwind the loop after the Turn's final write."""


class TurnRunner:
    def __init__(self, cid: str, tid: str, *, remaining_ms: Callable[[], int] = lambda: 10 ** 9,
                 sleep: Callable[[float], None] = time.sleep):
        self.cid, self.tid = cid, tid
        self.remaining_ms = remaining_ms
        self.sleep = sleep
        self.limits = limits_module.limits()
        turn = store.get_turn(cid, tid)
        if turn is None:
            raise LookupError(f'turn {tid} of conversation {cid} not found')
        self.turn = turn
        self.sub = turn['sub']
        self.meta = store.get_meta(cid) or {}
        self.principal = principal_from_claims(turn.get('claims') or {})
        self.config = settings.read_configuration(0)
        self.actions = settings.actions_enabled(self.config)
        self.page = dict(turn.get('page_context') or {})
        self.route = self.page.get('route') or '/'
        self.domain = guide.domain_for_route(self.route)
        self.chapter = guide.chapter_for_route(self.route)
        pending = turn.get('pending') or {}
        self.extra = set(pending.get('extra') or [])
        self.pending_messages: List[Dict[str, Any]] = list(pending.get('messages') or [])
        self.pending_tool_uses: List[Dict[str, Any]] = list(pending.get('tool_uses') or [])
        self.pending_results: List[Dict[str, Any]] = list(pending.get('results') or [])
        self.text: str = turn.get('text') or ''
        self.event_seq = len(turn.get('events') or [])
        self.usage = dict(turn.get('usage') or {'input': 0, 'output': 0, 'cache_read': 0,
                                                 'cache_write': 0})
        self.model_calls = int(turn.get('model_calls') or 0)
        self.tool_calls = int(turn.get('tool_calls') or 0)
        self.pipeline_calls = int(turn.get('pipeline_calls') or 0)
        self.confirmations = int(turn.get('confirmations') or 0)
        self.tool_activity: List[str] = list(pending.get('activity') or [])
        self.started = time.monotonic()
        self.turn_started_ms = int(turn.get('started_at') or store.now_ms())
        self.stop_requested = bool(turn.get('stop_requested'))
        self.budget_limit: Optional[str] = None
        self.tool_use_supported: Optional[bool] = None
        self.cache_points = bedrock.supports_cache_points(self._bedrock_config()['model_id'])
        self._last_progress = 0.0
        self._bedrock_client = None

    # ------------------------------------------------------------- entry

    def run(self, resume: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.turn.get('status') in store.TERMINAL_STATUSES:
            return self.turn
        try:
            if resume:
                self._resume(resume)
            else:
                self._start()
            self._loop()
        except TurnEnded:
            pass
        except Exception as error:  # noqa: BLE001 - every exit path persists a status
            logger.error('turn %s failed: %s', self.tid, type(error).__name__, exc_info=True)
            try:
                self._finish('failed', failure={'code': 'internal_error',
                                                'message': type(error).__name__})
            except TurnEnded:
                pass
        return store.get_turn(self.cid, self.tid) or self.turn

    # --------------------------------------------------------- lifecycle

    def _start(self) -> None:
        self.pending_messages = [{'role': 'user', 'content': [{'text': self.turn['prompt']}]}]
        self.turn_started_ms = store.now_ms()
        self._persist(status='running', started_at=self.turn_started_ms)

    def _resume(self, resume: Dict[str, Any]) -> None:
        binding_id = str(resume.get('binding_id') or '')
        binding = store.get_binding(self.sub, binding_id)
        confirmation = self.turn.get('confirmation') or {}
        if (binding is None or not binding.get('used') or binding.get('tid') != self.tid
                or confirmation.get('binding_id') != binding_id):
            self._fail('binding_invalid', 'the confirmation does not belong to this turn')
        expected = store.binding_hash(binding['tool'], binding['args'], self.sub, self.cid, self.tid)
        if binding.get('hash') != expected:
            self._fail('binding_invalid', 'the confirmation does not match the proposal')
        if not self.pending_tool_uses or self.pending_tool_uses[0].get('name') != binding['tool']:
            self._fail('binding_invalid', 'no proposal is pending for this confirmation')
        use = self.pending_tool_uses.pop(0)
        decision = binding.get('decision')
        self._event('confirmation_resolved', decision=decision, tool=binding['tool'])
        if decision == 'confirm':
            spec = tools.BY_NAME.get(binding['tool'])
            args = dict(binding['args'])
            confirm_value = pipeline.expected_confirm(spec, args)
            if confirm_value is not None:
                args['confirm'] = confirm_value
            block = self._run_catalog_tool(use, spec, args, mutating=True)
        else:
            block = tools.tool_result_block(use['toolUseId'], prompts.declined_payload(), True)
        self.pending_results.append(block)
        self._persist(status='running', confirmation=None)
        self._process_tool_uses()
        self._append_results_message()

    def _loop(self) -> None:
        while True:
            self._guard()
            response = self._model_call()
            self.pending_messages.append({'role': 'assistant', 'content': response.content
                                          or [{'text': ''}]})
            if not response.tool_uses:
                self._finish('completed', stop_reason=response.stop_reason)
            self.pending_tool_uses = list(response.tool_uses)
            self.pending_results = []
            self._persist()
            self._process_tool_uses()
            self._append_results_message()
            if self.budget_limit:
                self._finish('completed', budget=self.budget_limit)

    # ------------------------------------------------------------- guards

    def _guard(self) -> None:
        config = settings.read_configuration(0)
        if not config.get('enabled', True):
            self._fail('assistant_disabled', 'the assistant was disabled by an administrator')
        if store.now_s() >= int(self.turn.get('exp') or 0):
            self._fail('not_authenticated', 'the session expired; sign in again')
        if self.stop_requested:
            self._finish('stopped')
        elapsed = time.monotonic() - self.started
        turn_elapsed_s = (store.now_ms() - self.turn_started_ms) / 1000
        if turn_elapsed_s > self.limits['turn_seconds'] or elapsed > self.limits['turn_seconds'] \
                or self.remaining_ms() < MIN_REMAINING_MS:
            self._finish('completed', budget='turn_seconds')
        if self.model_calls >= self.limits['model_calls_per_turn']:
            self._finish('completed', budget='model_calls_per_turn')
        if self.usage.get('output', 0) >= self.limits['output_tokens_per_turn']:
            self._finish('completed', budget='output_tokens_per_turn')

    # -------------------------------------------------------------- model

    def _bedrock_config(self) -> Dict[str, Any]:
        if not hasattr(self, '_bedrock_config_cache'):
            self._bedrock_config_cache = bedrock_common.get_bedrock_configuration()
        return self._bedrock_config_cache

    def _client(self):
        if self._bedrock_client is None:
            self._bedrock_client = bedrock.client(self._bedrock_config())
        return self._bedrock_client

    def _build_request(self) -> Dict[str, Any]:
        config = self._bedrock_config()
        chapter_text = None
        if self.chapter:
            read = guide.read_chapter(self.chapter)
            chapter_text = read[0] if read else None
        user = self.principal.user
        system = prompts.build_system(
            index_text=guide.read_index(), chapter_text=chapter_text, chapter_name=self.chapter,
            page_context=self.page, principal_display=user.get('email') or self.principal.username,
            claimed_role=user.get('role', 'Viewer'), actions_enabled=self.actions,
            now_iso=dt.datetime.now(dt.timezone.utc).isoformat(timespec='minutes'),
            support_email=os.environ.get('ASSISTANT_SUPPORT_EMAIL') or None,
            documentation_url=os.environ.get('ASSISTANT_DOCUMENTATION_URL') or None,
            cache_points=self.cache_points)
        history = history_window(store.list_messages(self.cid), self.limits['window_tokens'])
        messages = merge_alternating(history + self.pending_messages)
        tool_config = None
        if self.tool_use_supported is not False:
            specs = tools.exposed_specs(self.domain, self.actions, self.extra)
            tool_config = tools.tool_config(specs, guide.list_chapters(), self.cache_points)
        inference = bedrock_common.build_inference_config(config)
        remaining_output = max(256, self.limits['output_tokens_per_turn']
                               - int(self.usage.get('output', 0)))
        inference['maxTokens'] = max(256, min(int(inference['maxTokens']), remaining_output))
        guardrail = {'guardrail_id': self.config.get('guardrail_id'),
                     'guardrail_version': self.config.get('guardrail_version')}
        return bedrock.build_request(model_id=config['model_id'], system=system, messages=messages,
                                     tool_config=tool_config, inference_config=inference,
                                     guardrail=guardrail)

    def _model_call(self) -> bedrock.ModelResponse:
        request = self._build_request()
        attempt = 0
        base_text = self.text
        while True:
            self.model_calls += 1
            streamed = {'text': ''}

            def on_text(delta: str, streamed=streamed, base_text=base_text) -> None:
                streamed['text'] += delta
                self.text = (base_text + streamed['text'])[:MAX_TEXT_CHARS]
                self._progress()

            try:
                response = bedrock.stream(self._client(), request, on_text)
            except bedrock.ToolUseUnsupported:
                settings.record_model_capability(request['modelId'], False)
                self.tool_use_supported = False
                self._event('error', code='tool_use_unsupported')
                request = dict(request)
                request.pop('toolConfig', None)
                self.text = base_text
                continue
            except bedrock.CachePointUnsupported:
                self.cache_points = False
                request = bedrock.strip_cache_points(request)
                self.text = base_text
                continue
            except bedrock.ModelError as error:
                if error.retryable and attempt < len(THROTTLE_BACKOFF_S):
                    self.sleep(THROTTLE_BACKOFF_S[attempt])
                    attempt += 1
                    self.text = base_text
                    continue
                self._event('error', code=error.code)
                self._fail(error.code, error.message)
            if self.tool_use_supported is None and 'toolConfig' in request:
                settings.record_model_capability(request['modelId'], True)
                self.tool_use_supported = True
            self.text = (base_text + response.text)[:MAX_TEXT_CHARS]
            for key in self.usage:
                self.usage[key] = int(self.usage.get(key, 0)) + int(response.usage.get(key, 0))
            store.add_tokens(self.sub, billable_tokens(response.usage))
            if response.stop_reason in ('guardrail_intervened', 'content_filtered'):
                self._event('error', code=response.stop_reason)
            if response.stop_reason == 'max_tokens':
                self._event('budget', limit='max_tokens')
            self._persist()
            return response

    # -------------------------------------------------------------- tools

    def _process_tool_uses(self) -> None:
        while self.pending_tool_uses:
            use = self.pending_tool_uses[0]
            self._check_stop()
            block = self._dispatch_tool_use(use)
            self.pending_results.append(block)
            self.pending_tool_uses.pop(0)
            self._persist()

    def _dispatch_tool_use(self, use: Dict[str, Any]) -> Dict[str, Any]:
        name = str(use.get('name') or '')
        args = dict(use.get('input') or {})
        tool_use_id = use.get('toolUseId') or ''
        if self.budget_limit:
            return tools.tool_result_block(tool_use_id, {
                'category': 'budget', 'message': f'the turn reached its {self.budget_limit} limit'},
                True)
        if self.tool_calls >= self.limits['tool_calls_per_turn'] \
                or self.pipeline_calls >= self.limits['pipeline_calls_per_turn']:
            self.budget_limit = 'tool_calls_per_turn'
            self._event('budget', limit=self.budget_limit)
            return tools.tool_result_block(tool_use_id, {
                'category': 'budget', 'message': 'the turn reached its tool call limit'}, True)
        self.tool_calls += 1
        if tools.is_local(name):
            return self._run_local_tool(use, name, args)
        spec = tools.BY_NAME.get(name)
        if spec is None or spec.phase not in tools.DELIVERED_PHASES:
            return tools.tool_result_block(tool_use_id, {
                'category': 'unknown_tool', 'message': f'{name} is not a tool'}, True)
        if spec.read_only:
            return self._run_catalog_tool(use, spec, args, mutating=False)
        args.pop('confirm', None)  # never the model's (design D7, Requirement 5.6)
        if not tools.mutating_allowed(spec, self.actions):
            category = 'mutating_tools_disabled' if not self.actions else 'not_available'
            return tools.tool_result_block(tool_use_id, {
                'category': category,
                'message': f'{spec.name} changes Portal state and is not available through the '
                           'assistant on this deployment; offer the Portal page instead'}, True)
        self._suspend_for_confirmation(use, spec, args)
        raise AssertionError('unreachable')  # pragma: no cover

    def _run_local_tool(self, use: Dict[str, Any], name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._event('tool_start', tool=name, title=name.replace('_', ' '),
                    args_summary=args_summary(args))
        started = time.monotonic()
        allowed = tools.allowed_specs(self.actions)

        def call(tool_name: str, tool_args: Dict[str, Any]):
            spec = tools.BY_NAME[tool_name]
            self.pipeline_calls += 1
            return self._call_pipeline(spec, tool_args, mutating=False)

        try:
            payload = tools.run_local_tool(name, args, call=call, allowed=allowed)
            if name == 'find_tools':
                self.extra.update(tools.find_names(payload))
            category = None
            is_error = False
        except tools.LocalToolError as error:
            payload, category, is_error = error.payload(), error.category, True
        self._event('tool_end', tool=name, category=category,
                    duration_ms=int((time.monotonic() - started) * 1000))
        self.tool_activity.append(f'{name}({args_summary(args)}) -> {category or "ok"}')
        return tools.tool_result_block(use['toolUseId'], payload, is_error)

    def _run_catalog_tool(self, use: Dict[str, Any], spec, args: Dict[str, Any], *,
                          mutating: bool) -> Dict[str, Any]:
        self._event('tool_start', tool=spec.name, title=spec.title, args_summary=args_summary(args))
        self.pipeline_calls += 1
        outcome = self._call_pipeline(spec, args, mutating=mutating)
        self._event('tool_end', tool=spec.name, category=outcome.category,
                    duration_ms=outcome.duration_ms, status=outcome.status)
        self.tool_activity.append(f'{spec.name}({args_summary(args)}) -> '
                                  f'{outcome.category or "ok"}')
        payload = tools.outcome_payload(outcome)
        if outcome.shaped is not None and outcome.shaped.resource_id and not outcome.is_error:
            payload['resource_id'] = outcome.shaped.resource_id
        if spec.status_tool and not spec.read_only:
            payload['status_tool'] = spec.status_tool
        return tools.tool_result_block(use['toolUseId'], payload, outcome.is_error)

    def _call_pipeline(self, spec, args: Dict[str, Any], *, mutating: bool):
        ctx = McpContext(
            request_id=f'{self.tid}/{self.pipeline_calls}', tool=spec.name,
            server=f'{SERVER_NAME}@{__version__}',
            client=self.turn.get('client_label') or 'dda-portal-frontend/unknown',
            source_ip=self.turn.get('source_ip'), user_agent=self.turn.get('user_agent'),
            principal_email=self.principal.email, channel='assistant')
        return pipeline.call_tool(spec, args, self.principal, ctx, mutating_enabled=mutating,
                                  server_version=__version__, event_name='assistant_tool')

    def _suspend_for_confirmation(self, use: Dict[str, Any], spec, args: Dict[str, Any]) -> None:
        binding = store.create_binding(sub=self.sub, cid=self.cid, tid=self.tid, tool=spec.name,
                                       args=args, seconds=self.limits['binding_seconds'])
        self.confirmations += 1
        card = {
            'binding_id': binding['binding_id'], 'tool': spec.name, 'title': spec.title,
            'description': spec.description, 'args': binding['args'],
            'usecase_id': args.get('usecase_id'), 'destructive': bool(spec.destructive),
            'expires_at': binding['expires_at'],
        }
        self._event('confirmation_requested', tool=spec.name, binding_id=binding['binding_id'])
        self._persist(status='awaiting_confirmation', confirmation=card)
        raise TurnEnded()

    def _append_results_message(self) -> None:
        if self.pending_results:
            self.pending_messages.append({'role': 'user', 'content': list(self.pending_results)})
        self.pending_results = []
        self._persist()

    # ------------------------------------------------------- persistence

    def _event(self, kind: str, **fields: Any) -> None:
        self.event_seq += 1
        event = {'seq': self.event_seq, 'ts': store.now_ms(), 'type': kind,
                 'text_offset': len(self.text)}
        event.update({k: v for k, v in fields.items() if v is not None})
        self._queued_events.append(event)

    @property
    def _queued_events(self) -> List[Dict[str, Any]]:
        if not hasattr(self, '_events_buffer'):
            self._events_buffer: List[Dict[str, Any]] = []
        return self._events_buffer

    def _pending(self) -> Dict[str, Any]:
        return {'messages': _bounded_messages(self.pending_messages),
                'tool_uses': self.pending_tool_uses,
                'results': _bounded_messages([{'content': self.pending_results}])[0]['content'],
                'extra': sorted(self.extra), 'activity': self.tool_activity[-40:]}

    def _progress(self) -> None:
        now = time.monotonic()
        if now - self._last_progress < PROGRESS_INTERVAL_S:
            return
        self._persist()

    def _persist(self, **attrs: Any) -> None:
        self._last_progress = time.monotonic()
        events = list(self._queued_events)
        self._events_buffer = []
        pending = attrs.pop('pending', None)
        item = store.append_progress(
            self.cid, self.tid, text=self.text, events=events,
            pending=self._pending() if pending is None else pending,
            usage=self.usage, model_calls=self.model_calls, tool_calls=self.tool_calls,
            pipeline_calls=self.pipeline_calls, confirmations=self.confirmations, **attrs)
        if item.get('stop_requested'):
            self.stop_requested = True
        self.turn = item

    def _check_stop(self) -> None:
        if self.stop_requested:
            self._finish('stopped')

    # -------------------------------------------------------------- end

    def _fail(self, code: str, message: str) -> None:
        self._finish('failed', failure={'code': code, 'message': message})

    def _finish(self, status: str, *, failure: Optional[Dict[str, str]] = None,
                budget: Optional[str] = None, stop_reason: Optional[str] = None) -> None:
        if budget:
            self._event('budget', limit=budget)
            self.text = (self.text + prompts.budget_sentence(budget))[:MAX_TEXT_CHARS]
        if status == 'stopped':
            self._event('stopped')
        finished = store.now_ms()
        self._persist(status=status, failure=failure, confirmation=None, finished_at=finished,
                      pending={'messages': [], 'tool_uses': [], 'results': [], 'extra': [],
                               'activity': []})
        self._store_messages(status, failure)
        store.release_live(self.sub)
        duration = finished - self.turn_started_ms
        telemetry.log_turn(sub=self.sub, conversation_id=self.cid, turn_id=self.tid, status=status,
                           failure_code=failure['code'] if failure else None,
                           model_calls=self.model_calls, tool_calls=self.tool_calls,
                           pipeline_calls=self.pipeline_calls, usage=self.usage,
                           duration_ms=duration, text_chars=len(self.text),
                           confirmations=self.confirmations, client=self.turn.get('client_label'),
                           version=__version__)
        telemetry.metrics(
            {'Turns': 1, 'ModelCalls': self.model_calls, 'ToolCalls': self.tool_calls,
             'InputTokens': self.usage.get('input', 0), 'OutputTokens': self.usage.get('output', 0),
             'CacheReadTokens': self.usage.get('cache_read', 0), 'TurnDurationMs': duration,
             'ModelErrors': 1 if failure and failure['code'].startswith('bedrock_')
             and failure['code'] != 'bedrock_throttled' else 0,
             'BedrockThrottled': 1 if failure and failure['code'] == 'bedrock_throttled' else 0},
            {'status': status}, {'TurnDurationMs': 'Milliseconds'})
        raise TurnEnded()

    def _store_messages(self, status: str, failure: Optional[Dict[str, str]]) -> None:
        retention = int(self.config.get('retention_days') or 30)
        turn_count = int(self.meta.get('turn_count') or 0)
        prompt = self.turn.get('prompt') or ''
        answer = self.text.strip()
        if not answer:
            answer = ('[The assistant did not answer: ' + (failure['code'] if failure else status)
                      + ']')
        content: List[Dict[str, Any]] = [{'text': answer}]
        if self.tool_activity:
            content.append({'text': '(Tools used in this turn: ' + '; '.join(self.tool_activity[-12:])
                            + ')'})
        events = list((store.get_turn(self.cid, self.tid) or {}).get('events') or [])
        messages = [
            {'role': 'user', 'content': [{'text': prompt}]},
            {'role': 'assistant', 'content': content,
             'display': {'text': self.text, 'events': events, 'status': status,
                         'failure': failure, 'turn_id': self.tid}},
        ]
        store.append_messages(self.cid, messages, first_seq=turn_count * 2, turn_id=self.tid,
                              retention_days=retention)
        store.touch_conversation(
            self.sub, self.cid, retention_days=retention,
            title=(prompt.strip()[:80] if turn_count == 0 and not self.meta.get('title') else None),
            turn_delta=1, size_delta=store.size_of(messages), live_turn_id=None)


# ------------------------------------------------------------------ helpers

def billable_tokens(usage: Dict[str, Any]) -> int:
    """The tokens one model call charges against `tokens_per_day`: input,
    output and cache-write tokens at full weight, cache reads at
    CACHE_READ_WEIGHT, rounded up so a call never counts as free."""
    full = sum(int(usage.get(key, 0) or 0) for key in ('input', 'output', 'cache_write'))
    cached = int(usage.get('cache_read', 0) or 0)
    return full + int(math.ceil(cached * CACHE_READ_WEIGHT))


def args_summary(args: Dict[str, Any]) -> str:
    """Argument names, with identifier-like values only (design §7)."""
    parts = []
    for key in sorted(args or {}):
        value = args[key]
        if key in IDENTIFIER_KEYS or key.endswith('_id') or key.endswith('_name'):
            if isinstance(value, (str, int)) and len(str(value)) <= 64:
                parts.append(f'{key}={value}')
                continue
        parts.append(key)
    return ', '.join(parts)


def _estimate_tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, default=str)) // 4)


def history_window(messages: List[Dict[str, Any]], budget_tokens: int) -> List[Dict[str, Any]]:
    """The newest stored messages that fit the budget, starting with a user
    message (design §8.1). Stored messages carry text only (tool activity is
    summarized at the end of each assistant message), so no toolUse /
    toolResult pair can be split."""
    kept: List[Dict[str, Any]] = []
    total = 0
    for item in sorted(messages, key=lambda m: int(m.get('seq', 0)), reverse=True):
        message = {'role': item['role'], 'content': list(item.get('content') or [])}
        cost = _estimate_tokens(message)
        if kept and total + cost > budget_tokens:
            break
        kept.append(message)
        total += cost
    kept.reverse()
    while kept and kept[0]['role'] != 'user':
        kept.pop(0)
    return kept


def merge_alternating(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Converse requires alternating roles: consecutive same-role messages are
    merged into one message with the concatenated content."""
    merged: List[Dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]['role'] == message['role']:
            merged[-1] = {'role': message['role'],
                          'content': list(merged[-1]['content']) + list(message['content'])}
        else:
            merged.append({'role': message['role'], 'content': list(message['content'])})
    return merged


def _bounded_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The pending messages as stored in the Turn item: each toolResult's JSON
    cut to STORED_RESULT_CHARS so the item stays far below the item limit."""
    bounded = []
    for message in messages:
        content = []
        for block in message.get('content') or []:
            if 'toolResult' in block:
                result = dict(block['toolResult'])
                parts = []
                for part in result.get('content') or []:
                    if 'json' in part:
                        text = json.dumps(part['json'], default=str)
                        parts.append({'json': part['json']} if len(text) <= STORED_RESULT_CHARS
                                     else {'text': text[:STORED_RESULT_CHARS] + ' ...[truncated]'})
                    elif 'text' in part:
                        parts.append({'text': str(part['text'])[:STORED_RESULT_CHARS]})
                result['content'] = parts
                content.append({'toolResult': result})
            else:
                content.append(block)
        bounded.append({**message, 'content': content})
    return bounded


def run(cid: str, tid: str, resume: Optional[Dict[str, Any]] = None,
        remaining_ms: Callable[[], int] = lambda: 10 ** 9) -> Dict[str, Any]:
    return TurnRunner(cid, tid, remaining_ms=remaining_ms).run(resume)
