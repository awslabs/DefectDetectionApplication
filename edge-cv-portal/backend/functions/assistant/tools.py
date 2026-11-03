"""Tool exposure and the assistant-local tools (design D5, D10, D11, §5;
Requirements 5.1-5.3, 7.3).

The model sees two kinds of tools: catalog tools (rendered from
`mcp_portal.catalog` with the catalog's own name, description and schema,
executed through `mcp_portal.pipeline.call_tool`) and three local tools
(`guide_read_section`, `journey_status`, `find_tools`) executed here.
"""
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from assistant import ACTIONS_DELIVERED, DESTRUCTIVE_ALLOWLIST, LOCAL_TOOL_NAMES
from assistant import guide, journey
from mcp_portal import DELIVERED_PHASES
from mcp_portal.catalog import CATALOG, render_description

MAX_RESULT_CHARS = 16_000
FIND_TOOLS_MAX = 10
_COMBINATORS = ('oneOf', 'anyOf', 'allOf')

BY_NAME = {spec.name: spec for spec in CATALOG}


# ----------------------------------------------------------------- exposure

def delivered_specs(phases: Sequence[int] = None) -> List[Any]:
    phases = tuple(DELIVERED_PHASES if phases is None else phases)
    return [spec for spec in CATALOG if spec.phase in phases]


def mutating_allowed(spec, actions_enabled: bool) -> bool:
    """Whether a mutating spec may be exposed at all (switch, phase gate and
    the destructive allow-list)."""
    if spec.read_only:
        return True
    if not (actions_enabled and ACTIONS_DELIVERED):
        return False
    return (not spec.destructive) or spec.name in DESTRUCTIVE_ALLOWLIST


def exposed_specs(page_domain: Optional[str], actions_enabled: bool,
                  extra: Iterable[str] = (), phases: Sequence[int] = None) -> List[Any]:
    """Design D5: every delivered read-only tool; with actions on, the allowed
    mutating tools of the page's domain plus the names in `extra` (the
    monotonic union of `find_tools` results within a Turn)."""
    extra = set(extra or ())
    specs = []
    for spec in delivered_specs(phases):
        if spec.read_only:
            specs.append(spec)
        elif mutating_allowed(spec, actions_enabled) and (
                spec.domain == page_domain or spec.name in extra):
            specs.append(spec)
    return specs


def allowed_specs(actions_enabled: bool, phases: Sequence[int] = None) -> List[Any]:
    """Every spec the switches allow, whatever the page (for `find_tools`)."""
    return [s for s in delivered_specs(phases) if mutating_allowed(s, actions_enabled)]


# ----------------------------------------------------------- Bedrock schema

def adapt_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """The catalog schema as Bedrock's tool `inputSchema.json` (design D11):
    a top-level `oneOf`/`anyOf`/`allOf` of `required` lists is dropped and
    stated in `description`; everything else is the catalog's. The pipeline
    still validates calls against the catalog schema."""
    adapted = {k: v for k, v in schema.items() if k not in _COMBINATORS}
    adapted.setdefault('type', 'object')
    notes = []
    for combinator in _COMBINATORS:
        branches = schema.get(combinator)
        if not branches:
            continue
        names = []
        for branch in branches:
            required = branch.get('required') if isinstance(branch, dict) else None
            if required:
                names.append(' and '.join(required))
        if not names:
            continue
        word = {'oneOf': 'exactly one of', 'anyOf': 'at least one of', 'allOf': 'all of'}[combinator]
        notes.append(f'Provide {word}: {", ".join(names)}.')
    if notes:
        description = (adapted.get('description') or '').rstrip()
        adapted['description'] = (description + ' ' + ' '.join(notes)).strip()
    return adapted


def bedrock_tool_spec(spec) -> Dict[str, Any]:
    return {'toolSpec': {
        'name': spec.name,
        'description': render_description(spec),
        'inputSchema': {'json': adapt_schema(spec.input_schema)},
    }}


def local_tool_specs(chapters: Sequence[str]) -> List[Dict[str, Any]]:
    """The three local tools (design §5), with the shipped chapter names."""
    chapter_names = list(chapters) + [guide.INDEX]
    return [
        {'toolSpec': {
            'name': 'guide_read_section',
            'description': ('Reads a chapter of the DDA user guide, or one section of it when '
                            '`section` names (part of) a heading. Use it for questions about '
                            'a page, field or procedure the current page chapter does not '
                            'cover. Returns the text and a guide: link to cite.'),
            'inputSchema': {'json': {
                'type': 'object',
                'properties': {
                    'chapter': {'type': 'string', 'enum': chapter_names,
                                'description': 'The chapter file name.'},
                    'section': {'type': 'string', 'maxLength': 200,
                                'description': 'Part of a heading; omit for the whole chapter.'},
                },
                'required': ['chapter'], 'additionalProperties': False}}}},
        {'toolSpec': {
            'name': 'journey_status',
            'description': ('Reports where a Use_Case stands on the end-to-end path (data, '
                            'labeling, training, workflows, builds, devices, deployments) and '
                            'the next step with its page and guide chapter. Call it when the '
                            'user asks what to do next or how far along they are.'),
            'inputSchema': {'json': {
                'type': 'object',
                'properties': {'usecase_id': {'type': 'string', 'maxLength': 128}},
                'required': ['usecase_id'], 'additionalProperties': False}}}},
        {'toolSpec': {
            'name': 'find_tools',
            'description': ('Searches the Portal tool catalog by keyword when none of the '
                            'listed tools fits a task. Returns up to 10 matching tools; they '
                            'become callable for the rest of this turn.'),
            'inputSchema': {'json': {
                'type': 'object',
                'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
                'required': ['query'], 'additionalProperties': False}}}},
    ]


def tool_config(specs: Sequence[Any], chapters: Sequence[str], cache_point: bool) -> Dict[str, Any]:
    tools = [bedrock_tool_spec(s) for s in specs] + local_tool_specs(chapters)
    if cache_point:
        tools.append({'cachePoint': {'type': 'default'}})
    return {'tools': tools}


# -------------------------------------------------------------- tool results

def _bounded_json(value: Any) -> Dict[str, Any]:
    text = json.dumps(value, ensure_ascii=True, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return {'json': value}
    return {'text': text[:MAX_RESULT_CHARS] + ' ...[truncated: the result exceeds '
            f'{MAX_RESULT_CHARS} characters; ask for fewer items]'}


def tool_result_block(tool_use_id: str, payload: Any, is_error: bool = False) -> Dict[str, Any]:
    """A Converse `toolResult` content block, JSON when it fits."""
    if isinstance(payload, (dict, list)):
        content = [_bounded_json(payload)]
    else:
        content = [{'text': str(payload)[:MAX_RESULT_CHARS]}]
    return {'toolResult': {'toolUseId': tool_use_id, 'content': content,
                           'status': 'error' if is_error else 'success'}}


def outcome_payload(outcome, request_id: Optional[str] = None) -> Dict[str, Any]:
    """The JSON the model receives for a pipeline outcome."""
    payload = dict(outcome.structured(request_id))
    payload.pop('request_id', None)
    return payload


# -------------------------------------------------------------- local tools

def find_tools(query: str, specs: Sequence[Any]) -> List[Dict[str, Any]]:
    words = [w for w in (query or '').lower().split() if w]
    if not words:
        return []
    scored = []
    for spec in specs:
        haystack = ' '.join((spec.name, spec.title, spec.description)).lower()
        score = sum(haystack.count(w) for w in words)
        if score:
            scored.append((-score, spec.name, spec))
    scored.sort()
    return [{'name': s.name, 'title': s.title, 'description': s.description,
             'classification': 'read-only' if s.read_only else
             ('mutating, destructive' if s.destructive else 'mutating')}
            for _score, _name, s in scored[:FIND_TOOLS_MAX]]


def is_local(name: str) -> bool:
    return name in LOCAL_TOOL_NAMES


def run_local_tool(name: str, args: Dict[str, Any], *, call: Callable[[str, Dict[str, Any]], Any],
                   allowed: Sequence[Any]) -> Any:
    """Execute a local tool; `call(tool_name, args)` runs a catalog tool
    through the pipeline (for `journey_status`). Returns a JSON payload or
    raises `LocalToolError`."""
    args = dict(args or {})
    if name == 'guide_read_section':
        chapter = str(args.get('chapter', ''))
        section = args.get('section')
        if not guide.valid_name(chapter):
            raise LocalToolError('invalid_input', 'chapter is not a guide chapter')
        if section:
            found = guide.read_section(chapter, str(section))
            if found is None:
                sections = [h for _level, h in guide.list_sections(chapter)][:40]
                raise LocalToolError('not_found', f'no section matching {section!r}; headings: '
                                     + '; '.join(sections))
            text, link = found
            return {'chapter': chapter, 'section': section, 'text': text, 'link': link}
        read = guide.read_chapter(chapter)
        if read is None:
            raise LocalToolError('not_found', 'chapter not found')
        text, truncated = read
        return {'chapter': chapter, 'text': text, 'truncated': truncated,
                'link': guide.guide_link(chapter)}
    if name == 'journey_status':
        usecase_id = args.get('usecase_id')
        if not isinstance(usecase_id, str) or not usecase_id.strip():
            raise LocalToolError('invalid_input', 'usecase_id is required')
        return journey.status(usecase_id.strip(), call)
    if name == 'find_tools':
        return {'tools': find_tools(str(args.get('query', '')), allowed)}
    raise LocalToolError('unknown_tool', f'{name} is not a tool')


class LocalToolError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(f'{category}: {message}')
        self.category = category
        self.message = message

    def payload(self) -> Dict[str, Any]:
        return {'category': self.category, 'message': self.message}


def find_names(result: Dict[str, Any]) -> List[str]:
    return [t['name'] for t in result.get('tools', []) if t.get('name') in BY_NAME]
