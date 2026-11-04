"""The user guide as the assistant's grounding (design D4, §5).

The chapters of `docs/user-guide/` ship in the `AssistantGuideLayer` and are
read from `ASSISTANT_GUIDE_DIR` (default `/opt/guide`). `route-chapters.json`
(next to this module) maps Portal route prefixes to chapters and domains;
the frontend shares the same file.
"""
import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

GUIDE_DIR_ENV = 'ASSISTANT_GUIDE_DIR'
DEFAULT_GUIDE_DIR = '/opt/guide'
INDEX = 'README.md'
MAX_CHARS = 12_000
CHAPTER_NAME = re.compile(r'^(README|[0-9]{2}-[a-z0-9-]+)\.md$')
_HEADING = re.compile(r'^(#{1,4})\s+(.*?)\s*$')

_MAP_PATH = os.path.join(os.path.dirname(__file__), 'route-chapters.json')


def guide_dir() -> str:
    return os.environ.get(GUIDE_DIR_ENV, DEFAULT_GUIDE_DIR)


@lru_cache(maxsize=1)
def route_map() -> Dict[str, object]:
    with open(_MAP_PATH, encoding='utf-8') as handle:
        data = json.load(handle)
    routes = sorted(data['routes'], key=lambda r: -len(r['prefix']))
    return {'routes': routes, 'non_route_chapters': tuple(data.get('non_route_chapters', ()))}


def _normalize(path: str) -> str:
    path = (path or '/').split('?', 1)[0].split('#', 1)[0]
    if not path.startswith('/'):
        path = '/' + path
    return path.rstrip('/') or '/'


def route_entry(path: str) -> Optional[Dict[str, str]]:
    """The longest matching route entry, or None."""
    path = _normalize(path)
    for entry in route_map()['routes']:  # longest prefix first
        prefix = entry['prefix']
        if path == prefix or path.startswith(prefix + '/'):
            return entry
    return None


def chapter_for_route(path: str) -> Optional[str]:
    entry = route_entry(path)
    return entry['chapter'] if entry else None


def domain_for_route(path: str) -> Optional[str]:
    entry = route_entry(path)
    return entry['domain'] if entry else None


def list_chapters() -> List[str]:
    """The chapter files present in the guide directory, index excluded."""
    try:
        names = os.listdir(guide_dir())
    except OSError:
        return []
    return sorted(n for n in names if CHAPTER_NAME.match(n) and n != INDEX)


def valid_name(name: str) -> bool:
    return bool(name) and bool(CHAPTER_NAME.match(name)) and os.sep not in name and '/' not in name


def _read(name: str) -> Optional[str]:
    if not valid_name(name):
        return None
    try:
        with open(os.path.join(guide_dir(), name), encoding='utf-8') as handle:
            return handle.read()
    except OSError:
        return None


def _bounded(text: str, limit: int = MAX_CHARS) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    newline = cut.rfind('\n')
    if newline > limit // 2:
        cut = cut[:newline]
    return cut + '\n\n[... truncated ...]', True


def read_index() -> str:
    return _bounded(_read(INDEX) or '', MAX_CHARS)[0]


def read_chapter(name: str) -> Optional[Tuple[str, bool]]:
    """(text, truncated) of a chapter, or None when it does not exist."""
    text = _read(name)
    if text is None:
        return None
    return _bounded(text)


def chapter_title(name: str) -> Optional[str]:
    text = _read(name)
    if text is None:
        return None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2)
    return name


def list_sections(name: str) -> List[Tuple[int, str]]:
    """(level, heading) of every heading in a chapter."""
    text = _read(name) or ''
    sections = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((len(match.group(1)), match.group(2)))
    return sections


def _slug(heading: str) -> str:
    """GitHub-style anchor of a heading."""
    slug = heading.strip().lower()
    slug = re.sub(r'[^\w\- ]', '', slug)
    slug = slug.replace(' ', '-')
    return slug


def guide_link(name: str, heading: Optional[str] = None) -> str:
    return f'guide:{name}' + (f'#{_slug(heading)}' if heading else '')


def read_section(name: str, heading: str) -> Optional[Tuple[str, str]]:
    """(text, link) of the section whose heading contains `heading`
    (case-insensitive), up to the next heading of the same or a higher
    level; None when the chapter or the section does not exist."""
    text = _read(name)
    if text is None or not heading:
        return None
    lines = text.splitlines()
    needle = heading.strip().lower()
    start = level = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and needle in match.group(2).lower():
            start, level, title = index, len(match.group(1)), match.group(2)
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = _HEADING.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    body, _ = _bounded('\n'.join(lines[start:end]))
    return body, guide_link(name, title)
