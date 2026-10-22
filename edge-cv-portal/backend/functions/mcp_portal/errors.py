"""Tool error results of the Portal_MCP_Server (design §2 `errors.py` row).

`ToolFailure(category, message, status=None)` renders as
`CallToolResult(is_error=True, content=[TextContent(f"{category}: {message}")],
structured_content={category, message, request_id, status, effect?,
status_tool?})` ("Tool error result (both servers)").

`effect_unknown_failure` (D32; review-4 M2): a mutating tool whose dispatch
ended in `timeout` or a post-dispatch `backend_failure` (the backing handler
may have run) reads "the operation may have been applied; call
<status_tool> before retrying" and carries `effect: "unknown"` and the
`status_tool`; a pre-dispatch failure or a read-only tool uses the plain
message, because its effect is known (none).

`map_backend_status` maps the backing route's HTTP status to a category and
passes the handler's `error` string through a scrub: text that looks like a
traceback is replaced by the category, bearer tokens and JWTs are removed.
"""
import json
import re
from typing import Optional

CATEGORIES = (
    'invalid_input', 'confirmation_required', 'not_authenticated', 'not_authorized',
    'authorization_unavailable', 'not_found', 'conflict', 'throttled', 'timeout',
    'backend_failure', 'result_too_large', 'mutating_tools_disabled',
)

_TRACEBACK_RE = re.compile(r'Traceback|File "')
# A credential ends where the next one starts ("Bearer", or a JWT's "eyJ"), so
# two adjacent ones are both removed.
_BEARER_RE = re.compile(r'Bearer\s+(?:(?!Bearer)[A-Za-z0-9._~+/=-])+', re.IGNORECASE)
_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.(?:(?!eyJ)[A-Za-z0-9_-])*')
MAX_MESSAGE_CHARS = 1000

BINDING_MESSAGE = 'no such object in the named Use_Case'
BUDGET_MESSAGE = 'insufficient time budget to start the backing operation; retry'


class ToolFailure(Exception):
    """A tool call that ends in an error result."""

    def __init__(self, category: str, message: str, status: Optional[int] = None,
                 effect: Optional[str] = None, status_tool: Optional[str] = None):
        super().__init__(f'{category}: {message}')
        self.category = category
        self.message = message
        self.status = status
        self.effect = effect
        self.status_tool = status_tool

    @property
    def text(self) -> str:
        return f'{self.category}: {self.message}'

    def structured(self, request_id: Optional[str]) -> dict:
        content = {'category': self.category, 'message': self.message,
                   'request_id': request_id, 'status': self.status}
        if self.effect is not None:
            content['effect'] = self.effect
        if self.status_tool is not None:
            content['status_tool'] = self.status_tool
        return content

    def to_result(self, request_id: Optional[str]):
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(is_error=True,
                              content=[TextContent(type='text', text=self.text)],
                              structured_content=self.structured(request_id))

    def __eq__(self, other):
        return isinstance(other, ToolFailure) and self.structured(None) == other.structured(None)

    def __hash__(self):
        return hash((self.category, self.message, self.status))


def effect_unknown_message(spec, category: str) -> str:
    """The full text of a mutating tool's unknown-effect failure."""
    return f'{category}: the operation may have been applied; call {spec.status_tool} before retrying'


def effect_unknown_failure(spec, category: str, message: str,
                           status: Optional[int] = None) -> ToolFailure:
    """A post-dispatch failure: unknown effect for a mutating tool, the plain
    `message` for a read-only one."""
    if spec.read_only:
        return ToolFailure(category, message, status)
    text = effect_unknown_message(spec, category)
    return ToolFailure(category, text[len(category) + 2:], status,
                       effect='unknown', status_tool=spec.status_tool)


def binding_failure() -> ToolFailure:
    """D36: a missing object and an object of another Use_Case answer alike."""
    return ToolFailure('not_found', BINDING_MESSAGE, status=404)


def category_for(status: int) -> str:
    if status in (400, 422):
        return 'invalid_input'
    if status == 401:
        return 'not_authenticated'
    if status == 403:
        return 'not_authorized'
    if status == 404:
        return 'not_found'
    if status == 409:
        return 'conflict'
    if status == 429:
        return 'throttled'
    if 400 <= status < 500:
        return 'invalid_input'
    return 'backend_failure'


def scrub(text: str, category: str) -> str:
    """The handler's message without tracebacks, bearer tokens or JWTs."""
    if not isinstance(text, str) or not text.strip():
        return category
    if _TRACEBACK_RE.search(text):
        return category
    text = _BEARER_RE.sub('[removed]', text)
    text = _JWT_RE.sub('[removed]', text)
    return text[:MAX_MESSAGE_CHARS]


def _error_text(body) -> Optional[str]:
    if isinstance(body, dict):
        for key in ('error', 'message', 'detail'):
            value = body.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = value.get('message') or value.get('error')
                if isinstance(nested, str):
                    return nested
        return None
    if isinstance(body, str):
        try:
            return _error_text(json.loads(body))
        except ValueError:
            return body
    return None


def map_backend_status(status: int, body) -> ToolFailure:
    """The error result of a backing route that answered `status` >= 300."""
    category = category_for(int(status))
    return ToolFailure(category, scrub(_error_text(body), category), status=int(status))
