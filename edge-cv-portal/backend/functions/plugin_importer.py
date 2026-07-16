"""
Plugin_Importer API Lambda function (Custom Node Designer)

Repository import orchestration (Requirements 4.1, 4.2, 4.3, 4.4, 4.5,
15.4, 15.5).

Routes (API Gateway REST):
    POST   /plugins/import      Import a GStreamer plugin from a public
                                repository, asynchronously: validate the
                                URL, create the Plugin_Record immediately
                                with import_status 'fetching' (provenance +
                                classification recorded), StartBuild the
                                lightweight CodeBuild fetch step (clone at
                                the requested revision, default branch when
                                omitted, sync the tree to
                                plugin-sources/{usecase_id}/{plugin_id}/{version}/)
                                WITHOUT polling, and answer 202 right away.
                                API Gateway REST integrations cap at 29 s,
                                so slow clones must never block the
                                response; the fetch outcome arrives via
                                EventBridge (handle_fetch_result below,
                                delegated from plugin_builds.py's result
                                handler) which scans buildability, advances
                                the record (failed / pending_selection /
                                imported), and queues + auto-starts builds
                                for the user-selected
                                Target_Architectures. The UI
                                polls GET /plugins/{id}/versions/{v} until
                                import_status leaves 'fetching'.
    POST   /plugins/{id}/versions/{v}/select-plugins
                                Record which of the enumerated individual
                                plugins to import for a plugin-set import
                                awaiting selection (import status
                                pending_selection): validates the selection
                                is non-empty and a subset of plugins_found,
                                records selected_plugins on the
                                Plugin_Record (and in provenance), and
                                submits builds for the previously requested
                                Target_Architectures. plugin_builds.py
                                passes the selection to CodeBuild as the
                                PLUGIN_TARGETS env override.
    POST   /plugins/{id}/versions/{v}/adjust-revision
                                Apply a per-platform source-revision
                                override to a settled imported plugin
                                (imported-plugin-revision-adjustment-fix):
                                fetch (or reuse) the adjusted revision's
                                tree into the record's `fetches` map,
                                map arch_revisions[arch] on fetch
                                success, and re-run the affected
                                platform's build. See adjust_revision.
    GET    /plugin-modules      Module_Listing (Requirement 6): fetch the
                                official GStreamer module index from
                                https://gstreamer.freedesktop.org/modules/,
                                parse it server-side into {name,
                                description, repoUrl, classification}
                                entries, cache the parsed index in the
                                ModuleIndexCache DynamoDB item with
                                fetchedAt and a 24-hour TTL, and reuse the
                                cached index for subsequent views (6.4).
                                Fetch/parse failure returns the distinct
                                MODULE_LISTING_UNAVAILABLE code so the UI
                                offers manual URL entry (6.3). Selecting a
                                module feeds its published repository
                                location (repoUrl) into POST
                                /plugins/import (6.2).

Flow (design "Plugin_Importer and Module_Listing", async import):

    1. Validate the repository URL and the selected Target_Architectures.
    2. Create the Plugin_Record (version 1, lifecycle dev, review
       pending - reusing plugin_records.new_version_item) with import
       provenance {repoUrl, revision, importedBy, importedAt,
       classification} via workflow_core's `classify_plugin_set` (4.2,
       15.4, 15.5) and import_status 'fetching' (no plugins_found yet).
    3. StartBuild on the lightweight fetch CodeBuild project
       (FETCH_PROJECT_NAME, env overrides REPO_URL / REVISION /
       DEST_PREFIX plus PLUGIN_ID / PLUGIN_VERSION / USECASE_ID so the
       result handler can attribute it) and answer 202 without polling.
    4. The EventBridge rule 'dda-portal-plugin-build-results' delivers
       the fetch build state change to plugin_builds.py, which delegates
       to `handle_fetch_result` here (same Lambda bundle). On SUCCEEDED
       the synced source tree is scanned for a GStreamer plugin build
       definition (meson.build / configure.ac with a plugin target, or a
       prebuilt .so; `scan_buildability` is a pure function over the
       file listing, design Property 4) and the record advances:
       unbuildable imports mark the record failed with the finding
       reported (4.5); buildable imports submit builds for the selected
       Target_Architectures (4.3) by queueing per-arch artifact entries
       and immediately starting them via
       plugin_builds.start_queued_builds (auto-start failure never
       fails the fetch-result handler), or wait in pending_selection
       for plugin sets. An unreachable repository or
       missing revision marks the record failed with a REPO_FETCH_FAILED
       finding — the record now exists (a deliberate change from the old
       synchronous no-record behavior) so the UI can show why (4.4).
       Result recording is idempotent on the fetch build id.

Error envelope: {"error": {"code", "message", "details"}} matching
plugin_records.py; 403 RBAC denials use the standard authorization
envelope (node-designer:import - UseCaseAdmin within the Use_Case or
PortalAdmin, 13.1/13.4).
"""
import json
import logging
import os
import posixpath
import re
import uuid
from html.parser import HTMLParser
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import boto3
import requests
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, Permission
)
from workflow_core.catalog import (
    DEVICE_ARCHITECTURES,
    DEEPSTREAM_ARCHITECTURES,
    classify_plugin_set,
)

# Reuse the Plugin_Record item shape, persistence helpers, and error
# envelope from plugin_records.py (same deployment bundle).
import plugin_records
from plugin_records import (
    authorize_record_access,
    can_manage,
    error_response,
    forbidden_response,
    get_version_item,
    new_version_item,
    not_found_response,
    now_ms,
    parse_body,
    plugin_table,
    source_s3_prefix,
    version_detail,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
codebuild = boto3.client('codebuild')
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# Environment variables. FETCH_PROJECT_NAME defaults to the fixed
# project name node-designer-stack.ts assigns, so handle_fetch_result
# also resolves it when this module runs inside the plugin_builds
# Lambda (whose environment does not carry the variable).
FETCH_PROJECT_NAME = os.environ.get('FETCH_PROJECT_NAME', 'dda-plugin-fetch')
PORTAL_ARTIFACTS_BUCKET = os.environ.get('PORTAL_ARTIFACTS_BUCKET')
MODULE_INDEX_CACHE_TABLE = os.environ.get('MODULE_INDEX_CACHE_TABLE')

# ---------------------------------------------------------------- constants

#: Revision recorded in provenance when the user omitted one and the
#: fetch cloned the repository default branch (4.1).
DEFAULT_REVISION = 'default'

#: Per-arch build status recorded when the import submits the source to
#: the Plugin_Build_Service (4.3). Once the record settles to
#: 'imported', the fetch-result path calls
#: plugin_builds.start_queued_builds, which StartBuilds the per-arch
#: projects (queued -> building); the EventBridge build results then
#: settle the status to succeeded/failed.
BUILD_QUEUED = 'queued'

#: Per-arch build status recorded when a post-import revision
#: adjustment's fetch fails: the affected architecture's entry settles
#: failed with the fetch-failure logTail
#: (_handle_adjustment_fetch_result), mirroring
#: plugin_builds.BUILD_FAILED.
BUILD_FAILED = 'failed'

#: Import outcome recorded on the Plugin_Record (4.5). A fresh import
#: starts in 'fetching' while the asynchronous CodeBuild fetch clones
#: the repository (the UI polls GET /plugins/{id}/versions/{v} until
#: the status leaves 'fetching'). A buildable plugin set with more than
#: one enumerated plugin waits in pending_selection until the user
#: picks the subset to import; the selection endpoint advances the
#: record to imported.
IMPORT_STATUS_FETCHING = 'fetching'
IMPORT_STATUS_IMPORTED = 'imported'
IMPORT_STATUS_FAILED = 'failed'
IMPORT_STATUS_PENDING_SELECTION = 'pending_selection'

#: Per-revision fetch status recorded in the `fetches` map of a
#: multi-revision import (arch_revisions). Each distinct effective
#: revision fetches once to its own rev-{slug}/ prefix; the record's
#: import_status leaves 'fetching' only when every fetch settles.
FETCH_STATUS_FETCHING = 'fetching'
FETCH_STATUS_SUCCEEDED = 'succeeded'
FETCH_STATUS_FAILED = 'failed'

#: import_finding recorded when the asynchronous fetch fails: the
#: repository was unreachable or the requested revision missing
#: (REPO_FETCH_FAILED semantics; the record exists so the UI can show
#: it — a deliberate change from the old synchronous no-record path).
FETCH_FAILURE_FINDING = (
    'Could not retrieve the repository: it is unreachable or the '
    'requested revision does not exist')

#: URL schemes accepted for public repository imports.
REPO_URL_SCHEMES = ('http', 'https', 'git')

#: Build-definition file names whose *content* the buildability scan
#: consults (every other file participates by name only).
BUILD_DEFINITION_FILES = ('meson.build', 'configure.ac', 'configure.in')

#: Maximum bytes of a build-definition file read for the scan.
MAX_BUILD_DEFINITION_BYTES = 256 * 1024

#: The official GStreamer Module_Listing page (Requirement 6.1).
MODULE_LISTING_URL = 'https://gstreamer.freedesktop.org/modules/'

#: Published repository location for an official module: the module's
#: freedesktop.org GitLab repository. Selecting a module feeds this URL
#: into the repository import path (6.2).
MODULE_REPO_URL_TEMPLATE = 'https://gitlab.freedesktop.org/gstreamer/{name}.git'

#: Single ModuleIndexCache item key holding the parsed index (6.4).
MODULE_INDEX_CACHE_KEY = 'gst-modules'

#: GitLab API tree endpoint of the gstreamer monorepo. One request per
#: plugin-set root (gst/, ext/, sys/) lists the module's individual
#: plugin directories for GET /plugin-modules?module=<name>.
MODULE_PLUGINS_TREE_URL = (
    'https://gitlab.freedesktop.org/api/v4/projects/'
    'gstreamer%2Fgstreamer/repository/tree')

#: GitLab tree page size (each per-module root stays under one page in
#: practice; pagination is followed via the x-next-page header anyway).
MODULE_PLUGINS_PER_PAGE = 100

#: ModuleIndexCache key prefix for one module's plugin list (same table
#: and 24-hour TTL pattern as the module index itself).
MODULE_PLUGINS_CACHE_KEY_PREFIX = 'gst-module-plugins/'

#: Module names accepted by the ?module= query parameter.
_MODULE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

#: Per-module plugin metadata the GStreamer monorepo ships at
#: subprojects/<module>/docs/gst_plugins_cache.json: a JSON object keyed
#: by plugin name whose values carry a "description" field. Joined onto
#: plugin listings as a display enhancement — never a blocker.
PLUGIN_DOCS_CACHE_FILENAME = 'gst_plugins_cache.json'

#: Raw GitLab location of one official module's plugin metadata cache.
MODULE_PLUGIN_DESCRIPTIONS_URL_TEMPLATE = (
    'https://gitlab.freedesktop.org/gstreamer/gstreamer/-/raw/main/'
    'subprojects/{module}/docs/' + PLUGIN_DOCS_CACHE_FILENAME)

#: Size cap for a gst_plugins_cache.json fetch/read (the file runs to
#: several MB for the large plugin sets). Anything over the cap is
#: dropped: entries simply lack descriptions.
MAX_PLUGIN_DESCRIPTION_CACHE_BYTES = 16 * 1024 * 1024

#: The cached index is reused for at most 24 hours (6.4). Also written
#: to the item's `ttl` attribute (epoch seconds) so DynamoDB expires it.
MODULE_INDEX_TTL_SECONDS = 24 * 60 * 60

#: HTTP timeout for the Module_Listing fetch.
MODULE_LISTING_TIMEOUT_SECONDS = float(
    os.environ.get('MODULE_LISTING_TIMEOUT_SECONDS', '10'))


# ------------------------------------------------- pure: URL validation

def validate_repo_url(repo_url: Optional[str]) -> Optional[str]:
    """
    Validate a public repository URL for import; returns an error
    message, or None when the URL is acceptable.

    Accepted: http / https / git URLs with a hostname (4.1). Anything
    else (file paths, ssh scp-style strings, missing host, embedded
    whitespace) is rejected before any fetch is attempted.
    """
    if not repo_url or not isinstance(repo_url, str):
        return 'repo_url is required'
    if any(c.isspace() for c in repo_url):
        return 'repo_url must not contain whitespace'
    try:
        parts = urlsplit(repo_url)
    except ValueError:
        return 'repo_url is not a valid URL'
    if parts.scheme.lower() not in REPO_URL_SCHEMES:
        return (f"repo_url scheme must be one of: {', '.join(REPO_URL_SCHEMES)}")
    if not parts.hostname:
        return 'repo_url must include a host'
    return None


def default_plugin_name(repo_url: str) -> str:
    """Plugin name derived from the repository URL's last path segment
    (``.git`` suffix stripped), falling back to the host."""
    parts = urlsplit(repo_url)
    segments = [s for s in parts.path.split('/') if s]
    if segments:
        name = segments[-1]
        if name.endswith('.git'):
            name = name[:-len('.git')]
        if name:
            return name
    return parts.hostname or 'imported-plugin'


def derive_import_name(explicit_name: Optional[str], repo_url: str,
                       selected_plugins: List[str]) -> str:
    """
    Record name for POST /plugins/import. An explicitly provided name
    always wins. Otherwise the URL-derived base name is used — and when
    the import-time selection contains exactly one plugin, the plugin
    is appended ("{base}-{plugin}", e.g. "gst-plugins-good-rtsp") so
    the record shows which plugin was imported rather than looking like
    the whole plugin set. Multi-plugin or absent selections keep the
    base name. Hyphenated names stay sanitize-safe for the S3 artifact
    keys plugin_builds.sanitize_plugin_name derives.
    """
    if explicit_name:
        return explicit_name
    base = default_plugin_name(repo_url)
    if len(selected_plugins) == 1:
        return f'{base}-{selected_plugins[0]}'
    return base


def selection_rename(current_name: Optional[str],
                     repo_url: Optional[str],
                     selected: List[str]) -> Optional[str]:
    """
    New record name for a post-fetch plugin selection
    (POST /plugins/{id}/versions/{v}/select-plugins), or None when the
    name should stay. A single-plugin selection renames the record to
    "{current_name}-{plugin}" so the library and detail views show
    which plugin was imported — but only while the name is still the
    URL-derived default: an explicitly chosen name is never overwritten.
    Without a provenance repoUrl the default cannot be recomputed, so
    the rename applies unconditionally on a single-plugin selection.
    """
    if len(selected) != 1:
        return None
    if repo_url and current_name != default_plugin_name(repo_url):
        return None  # explicitly named record: keep the name
    plugin = selected[0]
    return f'{current_name}-{plugin}' if current_name else plugin


# --------------------------------------------- pure: buildability scan
#
# The scan is a pure function over a source-tree file mapping
# {relative_path: content-or-None} so it is unit- and property-testable
# without AWS (design Property 4). Content is only consulted for the
# BUILD_DEFINITION_FILES; all other entries may map to None.
#
# A tree contains a buildable GStreamer_Plugin iff at least one of:
#   (a) prebuilt binary: a file whose name ends with ``.so`` exists;
#   (b) meson: a ``meson.build`` file declares a plugin library target
#       (a ``library(`` / ``shared_library(`` / ``shared_module(`` call)
#       AND references GStreamer (``gstreamer-1.0``, a ``gst_*`` /
#       ``gst-plugin`` dependency identifier);
#   (c) autotools: a ``configure.ac`` / ``configure.in`` file references
#       GStreamer (``gstreamer-1.0`` or a ``GST_PLUGIN`` / ``AG_GST_``
#       macro).

_MESON_TARGET_RE = re.compile(r'\b(?:shared_library|shared_module|library)\s*\(')
_MESON_GST_RE = re.compile(r'gstreamer-1\.0|gst-plugin|\bgst_\w+', re.IGNORECASE)
_AUTOTOOLS_GST_RE = re.compile(r'gstreamer-1\.0|GST_PLUGIN|AG_GST_')


def _meson_declares_plugin_target(content: str) -> bool:
    """meson.build content declares a GStreamer plugin library target"""
    return bool(_MESON_TARGET_RE.search(content)) and bool(_MESON_GST_RE.search(content))


def _autotools_declares_plugin_target(content: str) -> bool:
    """configure.ac/.in content references a GStreamer plugin build"""
    return bool(_AUTOTOOLS_GST_RE.search(content))


def scan_buildability(files: Mapping[str, Optional[str]]) -> Dict:
    """
    Scan a source-tree file mapping for a GStreamer plugin build
    definition (4.5).

    Returns
        {'buildable': bool,
         'kind': 'prebuilt' | 'meson' | 'autotools' | None,
         'evidence': [matching relative paths],
         'finding': str}  # human-readable, non-empty when unbuildable
    """
    prebuilt: List[str] = []
    meson_hits: List[str] = []
    autotools_hits: List[str] = []
    definition_files_seen: List[str] = []

    for path in sorted(files):
        name = posixpath.basename(path)
        if name.endswith('.so'):
            prebuilt.append(path)
            continue
        content = files.get(path)
        if name == 'meson.build':
            definition_files_seen.append(path)
            if content and _meson_declares_plugin_target(content):
                meson_hits.append(path)
        elif name in ('configure.ac', 'configure.in'):
            definition_files_seen.append(path)
            if content and _autotools_declares_plugin_target(content):
                autotools_hits.append(path)

    if prebuilt:
        return {'buildable': True, 'kind': 'prebuilt', 'evidence': prebuilt,
                'finding': ''}
    if meson_hits:
        return {'buildable': True, 'kind': 'meson', 'evidence': meson_hits,
                'finding': ''}
    if autotools_hits:
        return {'buildable': True, 'kind': 'autotools',
                'evidence': autotools_hits, 'finding': ''}

    if definition_files_seen:
        finding = (
            'No GStreamer plugin build definition found: '
            f"{', '.join(definition_files_seen)} present but none declares "
            'a GStreamer plugin target, and no prebuilt .so binary exists'
        )
    else:
        finding = (
            'No GStreamer plugin build definition found: the source tree '
            'contains no meson.build or configure.ac declaring a GStreamer '
            'plugin target and no prebuilt .so binary'
        )
    return {'buildable': False, 'kind': None, 'evidence': [], 'finding': finding}


# ------------------------------------------ pure: plugin enumeration
#
# The official GStreamer plugin sets (gst-plugins-good/bad/ugly - and any
# repository following the same meson monorepo layout) carry dozens of
# individual plugins, one per directory under gst/, ext/, and sys/, each
# with its own meson.build defining a plugin library target. Rather than
# bulk-importing the entire set, the import enumerates those individual
# plugin targets so the user can select which ones to import.
#
# Like scan_buildability, the enumeration is a pure function over the
# source-tree file mapping {relative_path: content-or-None}: content is
# only consulted for meson.build files (which list_source_tree fetches).

#: Monorepo directories whose immediate subdirectories are individual
#: plugin targets in the gst-plugins-good/bad/ugly layout.
PLUGIN_SET_ROOTS = ('gst', 'ext', 'sys')

_PLUGIN_DIR_MESON_RE = re.compile(
    r'^(?:' + '|'.join(PLUGIN_SET_ROOTS) + r')/([^/]+)/meson\.build$')


# ------------------------------------- pure: plugin descriptions join
#
# Per-plugin descriptions come from the gst_plugins_cache.json metadata
# the GStreamer modules ship under docs/. The parse and the join are
# pure functions so they are unit-testable, and every failure path
# (malformed JSON, wrong shape, missing fields) degrades to entries
# without descriptions — descriptions never fail or block a listing or
# an import.

def plugin_descriptions_from_cache(cache_json) -> Dict[str, str]:
    """
    {plugin_name: description} parsed from gst_plugins_cache.json
    content (a JSON object keyed by plugin name whose values carry a
    "description" field). Accepts the raw text or an already-parsed
    mapping. Malformed or unexpected content parses to {} — never
    raises.
    """
    if isinstance(cache_json, bytes):
        cache_json = cache_json.decode('utf-8', errors='replace')
    if isinstance(cache_json, str):
        try:
            cache_json = json.loads(cache_json)
        except ValueError:
            return {}
    if not isinstance(cache_json, dict):
        return {}
    descriptions: Dict[str, str] = {}
    for name, meta in cache_json.items():
        if not (isinstance(name, str) and name and isinstance(meta, dict)):
            continue
        description = meta.get('description')
        if isinstance(description, str) and description.strip():
            descriptions[name] = description.strip()
    return descriptions


def join_plugin_descriptions(entries: List[Dict],
                             descriptions: Mapping[str, str]) -> List[Dict]:
    """Plugin entries with a 'description' joined on by name where one
    is known; entries without a known description are unchanged (the
    key stays absent). The input entries are not mutated."""
    joined: List[Dict] = []
    for entry in entries:
        entry = dict(entry)
        description = descriptions.get(entry.get('name'))
        if description:
            entry['description'] = description
        joined.append(entry)
    return joined


def tree_plugin_descriptions(
        files: Mapping[str, Optional[str]]) -> Dict[str, str]:
    """{plugin_name: description} merged from every
    gst_plugins_cache.json whose content is present in the source-tree
    file mapping (typically docs/gst_plugins_cache.json)."""
    descriptions: Dict[str, str] = {}
    for path in sorted(files):
        if posixpath.basename(path) != PLUGIN_DOCS_CACHE_FILENAME:
            continue
        content = files.get(path)
        if content:
            descriptions.update(plugin_descriptions_from_cache(content))
    return descriptions


def enumerate_plugins(files: Mapping[str, Optional[str]],
                      single_plugin_name: str = 'plugin') -> List[Dict]:
    """
    Enumerate the individual plugin targets in a synced source tree.

    Returns [{'name', 'path', 'description'?}] entries — 'description'
    joined from any gst_plugins_cache.json present in the tree, absent
    when unknown:
      - plugin-set layout (gst-plugins-good style): one entry per
        directory exactly at {gst|ext|sys}/{plugin}/ whose meson.build
        declares a GStreamer plugin library target; `name` is the
        directory name, `path` the directory relative path;
      - single-plugin repository (buildable per scan_buildability, but
        no plugin-set entries): one entry named `single_plugin_name`
        with path '' - such imports skip the selection step;
      - tree with no buildable plugin: [].
    """
    entries: List[Dict] = []
    for path in sorted(files):
        match = _PLUGIN_DIR_MESON_RE.match(path)
        if not match:
            continue
        content = files.get(path)
        if content and _meson_declares_plugin_target(content):
            entries.append({'name': match.group(1),
                            'path': posixpath.dirname(path)})
    if not entries:
        if scan_buildability(files)['buildable']:
            entries = [{'name': single_plugin_name, 'path': ''}]
        else:
            return []
    return join_plugin_descriptions(entries, tree_plugin_descriptions(files))


def validate_plugin_selection(selected, found_names: List[str]) -> Optional[str]:
    """
    Validate a plugin selection against the enumerated plugins; returns
    an error message, or None when acceptable. The selection must be a
    non-empty list of plugin names and a subset of what the import
    enumeration found.
    """
    if not isinstance(selected, list) or not selected:
        return 'selected_plugins must be a non-empty list of plugin names'
    if not all(isinstance(name, str) and name for name in selected):
        return 'selected_plugins entries must be non-empty strings'
    unknown = sorted(set(selected) - set(found_names))
    if unknown:
        return ('selected_plugins must be a subset of the plugins found '
                f"by the import; unknown: {', '.join(unknown)}")
    return None


def validate_import_selected_plugins(selected) -> Optional[str]:
    """
    Validate the optional import-time `selected_plugins` of POST
    /plugins/import (sourced from GET /plugin-modules?module=...);
    returns an error message, or None when acceptable. Absent or empty
    means the whole module (today's behavior); when present it must be
    a list of non-empty plugin-name strings.
    """
    if selected is None:
        return None
    if not isinstance(selected, list):
        return 'selected_plugins must be a list of plugin names'
    if not all(isinstance(name, str) and name for name in selected):
        return 'selected_plugins entries must be non-empty strings'
    return None


def dedupe_selected_plugins(selected) -> List[str]:
    """The selection de-duplicated with its original order preserved."""
    seen = set()
    result: List[str] = []
    for name in selected or []:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


# -------------------------------------- pure: per-arch revision plan
#
# Optional per-architecture source revisions (POST /plugins/import
# `arch_revisions: {arch: revision}`): importing e.g. gst-plugins-good
# for all platforms needs different branches per platform generation
# (main for the GStreamer 1.20+ platforms, '1.16' for arm64_jp5, '1.14'
# for arm64_jp4). Each arch's effective revision is its override or the
# top-level revision (default branch when neither is given). When more
# than one DISTINCT effective revision exists, each distinct revision
# fetches ONCE to its own rev-{slug}/ prefix and archs sharing a
# revision share the tree; a single distinct revision keeps today's
# single-fetch flat layout exactly (compatibility with source
# inspection, node-generator acceptance, and existing records).

def validate_arch_revisions(arch_revisions, architectures: List[str]
                            ) -> Optional[str]:
    """
    Validate the optional `arch_revisions` of POST /plugins/import;
    returns an error message, or None when acceptable. Absent means
    the single top-level revision applies everywhere (today's
    behavior); when present it must map a subset of the requested
    Target_Architectures to non-empty revision strings.
    """
    if arch_revisions is None:
        return None
    if not isinstance(arch_revisions, dict):
        return ('arch_revisions must map Target_Architectures to '
                'revision strings')
    unknown = sorted(str(a) for a in set(arch_revisions) - set(architectures))
    if unknown:
        return ('arch_revisions keys must be requested '
                f"Target_Architectures; unknown: {', '.join(unknown)}")
    invalid = sorted(arch for arch, rev in arch_revisions.items()
                     if not isinstance(rev, str) or not rev.strip())
    if invalid:
        return ('arch_revisions values must be non-empty revision '
                f"strings; invalid: {', '.join(invalid)}")
    return None


def revision_slug(revision: Optional[str]) -> str:
    """
    S3-key-safe slug of one revision: DEFAULT_REVISION ('default') for
    an absent revision (the repository default branch), otherwise the
    revision with every non [A-Za-z0-9._-] run collapsed to '-' (e.g.
    '1.16' -> '1.16', 'feature/x' -> 'feature-x'). Pure.
    """
    if not revision:
        return DEFAULT_REVISION
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', revision).strip('-.')
    return slug or 'rev'


def revision_fetch_plan(revision: Optional[str], arch_revisions: Dict,
                        architectures: List[str],
                        base_prefix: str) -> Dict:
    """
    Fetch plan for an import's effective per-arch revisions. Pure.

    Returns {'mode': 'single', 'revision': rev-or-None} when at most
    one distinct effective revision exists — one fetch of that revision
    into the flat source_s3_prefix layout, exactly today's behavior.

    Otherwise {'mode': 'multi', 'fetches': {slug: {revision,
    source_prefix, status}}, 'arch_revisions': {arch: slug},
    'default_slug': slug}: one fetch per distinct revision into
    {base_prefix}rev-{slug}/, every requested arch mapped to its slug,
    and the default slug (the top-level revision's when among the
    fetches, the first slug deterministically otherwise) naming the
    tree the buildability scan and plugin enumeration run on. Slug
    collisions between distinct revisions disambiguate with a numeric
    suffix.
    """
    overrides = {arch: rev.strip()
                 for arch, rev in (arch_revisions or {}).items()}
    effective = {arch: overrides.get(arch) or revision or ''
                 for arch in architectures}
    distinct = sorted(set(effective.values()))
    if len(distinct) <= 1:
        single = distinct[0] if distinct else (revision or '')
        return {'mode': 'single', 'revision': single or None}

    slug_by_revision: Dict[str, str] = {}
    used = set()
    for rev in distinct:
        slug = base = revision_slug(rev or None)
        suffix = 2
        while slug in used:
            slug = f'{base}-{suffix}'
            suffix += 1
        used.add(slug)
        slug_by_revision[rev] = slug

    fetches = {
        slug: {
            'revision': rev or DEFAULT_REVISION,
            'source_prefix': f'{base_prefix}rev-{slug}/',
            'status': FETCH_STATUS_FETCHING,
        }
        for rev, slug in slug_by_revision.items()
    }
    arch_slugs = {arch: slug_by_revision[rev]
                  for arch, rev in effective.items()}
    default_slug = slug_by_revision.get(revision or '',
                                        sorted(fetches)[0])
    return {'mode': 'multi', 'fetches': fetches,
            'arch_revisions': arch_slugs, 'default_slug': default_slug}


def multi_fetch_failure_finding(failed_revisions: List[str]) -> str:
    """import_finding for a multi-revision import with failed fetches,
    naming the failing revision(s) (the per-fetch statuses stay on the
    record so the UI can show which trees did sync)."""
    plural = 's' if len(failed_revisions) != 1 else ''
    return ('Could not retrieve the repository at revision'
            f"{plural} {', '.join(failed_revisions)}: it is unreachable "
            'or the requested revision does not exist')


def adjustment_fetch_failure_log_tail(revision: str) -> str:
    """logTail recorded on an adjusted architecture's artifact entry
    when the adjustment's fetch fails (2.4). Pure."""
    return (f'The adjusted revision {revision} could not be fetched: '
            'the repository is unreachable or the revision does not exist')


#: adjustment_fetch_slot actions: reuse an already-synced succeeded
#: tree (no fetch), join a concurrent adjustment fetch of the same
#: revision (no second fetch), or fetch the revision (a fresh slug, or
#: a previously failed entry reset in place).
ADJUST_REUSE = 'reuse'
ADJUST_JOIN = 'join'
ADJUST_FETCH = 'fetch'


def adjustment_fetch_slot(item: Dict, revision: str) -> Tuple[str, str]:
    """
    Resolve the `fetches` slot a post-import revision adjustment
    targets for `revision`. Pure over the Plugin_Record item.

    Returns (slug, action) where action is one of:
      - ADJUST_REUSE: an existing entry records the same revision with
        status 'succeeded' — the synced tree is reused, no new fetch;
      - ADJUST_JOIN: an existing entry records the same revision with
        status 'fetching' (a concurrent adjustment) — the arch joins
        its pending_archs, no second fetch;
      - ADJUST_FETCH: the revision needs fetching — either into an
        existing entry recording the same revision whose fetch failed
        (reset in place, same slug), or under a fresh slug allocated
        via revision_slug with numeric-suffix collision disambiguation
        against the existing slugs (exactly like revision_fetch_plan),
        so an entry recording a DIFFERENT revision is never clobbered.
    """
    fetches = item.get('fetches') or {}
    for slug in sorted(fetches):
        entry = fetches[slug] or {}
        if entry.get('revision') != revision:
            continue
        status = entry.get('status')
        if status == FETCH_STATUS_SUCCEEDED:
            return slug, ADJUST_REUSE
        if status == FETCH_STATUS_FETCHING:
            return slug, ADJUST_JOIN
        return slug, ADJUST_FETCH  # failed entry: re-fetch in place

    slug = base = revision_slug(revision)
    suffix = 2
    while slug in fetches:
        slug = f'{base}-{suffix}'
        suffix += 1
    return slug, ADJUST_FETCH


# ------------------------------------------------- pure: provenance

def import_provenance(repo_url: str, revision: Optional[str],
                      module_name: Optional[str], user_id: str,
                      timestamp: int) -> Dict:
    """
    Import provenance for the Plugin_Record (4.2, 15.5): repository URL,
    retrieved revision (DEFAULT_REVISION when the default branch was
    used), importing user, retrieval timestamp, and the
    Plugin_Set_Classification derived via `classify_plugin_set` (15.4).
    """
    provenance = {
        'repoUrl': repo_url,
        'revision': revision or DEFAULT_REVISION,
        'importedBy': user_id,
        'importedAt': timestamp,
        'classification': classify_plugin_set(module_name, repo_url),
    }
    if module_name:
        provenance['moduleName'] = module_name
    return provenance


# ------------------------------------------- pure: Module_Listing parse
#
# The parse is a pure function over the fetched page content
# (`parse_module_listing`) so it is unit- and property-testable without
# AWS or the network (design Property 5, task 4.5).
#
# The listing page renders the module index as the HTML table whose
# header row starts with a "module" column; each module row's first
# cell links the module page (anchor text = module name) and the second
# cell carries the description. Layout/navigation tables on the page
# have no such header row and are ignored.

class ModuleListingParseError(ValueError):
    """The Module_Listing page could not be parsed into a module index."""


def module_repo_url(name: str) -> str:
    """The official module's published repository location (6.2)."""
    return MODULE_REPO_URL_TEMPLATE.format(name=name)


class _ModuleTableParser(HTMLParser):
    """Collects every <table> on the page as rows of (cell_tag, text).

    Tables are tracked on a stack, so the nested layout tables on the
    listing page each surface as their own entry in `tables`.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[Tuple[str, str]]]] = []
        self._table_stack: List[List[List[Tuple[str, str]]]] = []
        self._row: Optional[List[Tuple[str, str]]] = None
        self._cell: Optional[List[str]] = None
        self._cell_tag: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self._table_stack.append([])
        elif tag == 'tr' and self._table_stack:
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []
            self._cell_tag = tag

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._cell is not None:
            text = ' '.join(''.join(self._cell).split())
            self._row.append((self._cell_tag, text))
            self._cell = None
            self._cell_tag = None
        elif tag == 'tr' and self._row is not None:
            if self._table_stack and self._row:
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == 'table' and self._table_stack:
            self.tables.append(self._table_stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_module_listing(page: str) -> List[Dict]:
    """
    Parse the Module_Listing page content into the module index (6.1).

    Returns [{name, description, repoUrl, classification}] — one entry
    per module row, classification via `classify_plugin_set` over the
    module name and its published repository location (15.1).

    Raises ModuleListingParseError when the content contains no
    parseable module table (an unparseable response, 6.3).
    """
    if not isinstance(page, str):
        raise ModuleListingParseError('page content is not text')

    parser = _ModuleTableParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception as exc:  # HTMLParser raises on malformed markup
        raise ModuleListingParseError(f'malformed HTML: {exc}') from exc

    for table in parser.tables:
        header = next(
            (row for row in table if row and row[0][0] == 'th'), None)
        if not header or header[0][1].lower() != 'module':
            continue

        modules = []
        for row in table:
            if not row or row[0][0] != 'td':
                continue
            name = row[0][1]
            if not name:
                continue
            description = row[1][1] if len(row) > 1 else ''
            repo_url = module_repo_url(name)
            modules.append({
                'name': name,
                'description': description,
                'repoUrl': repo_url,
                'classification': classify_plugin_set(name, repo_url),
            })
        if modules:
            return modules

    raise ModuleListingParseError(
        'the page contains no parseable module table')


# --------------------------------------------- Module_Listing cache/fetch

def module_cache_table():
    """The ModuleIndexCache DynamoDB table"""
    return dynamodb.Table(MODULE_INDEX_CACHE_TABLE)


def read_cached_module_index() -> Optional[Dict]:
    """The cached ModuleIndexCache item, or None (missing or unreadable)."""
    try:
        response = module_cache_table().get_item(
            Key={'cache_key': MODULE_INDEX_CACHE_KEY})
    except Exception as exc:
        logger.warning(f'ModuleIndexCache read failed: {exc}')
        return None
    return response.get('Item')


def module_index_is_fresh(item: Dict, now_millis: int) -> bool:
    """The cached index is reused for at most 24 hours after fetchedAt
    (6.4)."""
    fetched_at = item.get('fetchedAt')
    if fetched_at is None:
        return False
    return (now_millis - int(fetched_at)) < MODULE_INDEX_TTL_SECONDS * 1000


def write_module_index_cache(modules: List[Dict], fetched_at_ms: int) -> None:
    """Cache the parsed index with fetchedAt and the 24-hour TTL (6.4).
    A cache-write failure never fails the request that fetched the index."""
    try:
        module_cache_table().put_item(Item={
            'cache_key': MODULE_INDEX_CACHE_KEY,
            'modules': modules,
            'fetchedAt': fetched_at_ms,
            'ttl': fetched_at_ms // 1000 + MODULE_INDEX_TTL_SECONDS,
        })
    except Exception as exc:
        logger.warning(f'ModuleIndexCache write failed: {exc}')


def fetch_module_listing() -> str:
    """Fetch the Module_Listing page content (6.1); raises on any HTTP
    failure."""
    response = requests.get(MODULE_LISTING_URL,
                            timeout=MODULE_LISTING_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


# ------------------------------------ per-module plugin list (?module=)
#
# GET /plugin-modules?module=<name> returns the individual plugins of
# one official module so the import view offers a plugin selection
# before the import (rather than always importing the whole set). The
# list is sourced from the GitLab API for the gstreamer monorepo: each
# immediate subdirectory of subprojects/<module>/{gst,ext,sys}/ is one
# plugin. Like the module index, the parse is a pure function
# (`module_plugins_from_trees`) over the fetched tree listings.

def module_plugins_from_trees(
        trees: Mapping[str, List[Dict]]) -> List[Dict]:
    """
    Parse GitLab repository-tree listings (one per plugin-set root)
    into the module's individual plugin entries [{'name': ...}]: every
    'tree' (directory) entry is one plugin; blobs and anything without
    a name are ignored. Name-sorted and de-duplicated across roots.
    """
    names = set()
    for root in PLUGIN_SET_ROOTS:
        for entry in trees.get(root) or []:
            if (isinstance(entry, dict) and entry.get('type') == 'tree'
                    and entry.get('name')):
                names.add(str(entry['name']))
    return [{'name': name} for name in sorted(names)]


def fetch_module_plugin_trees(module: str) -> Dict[str, List[Dict]]:
    """
    Fetch the GitLab tree listings of subprojects/<module>/{gst,ext,sys}
    (one plugin per subdirectory). A root the module does not carry
    (404) lists as empty; any other HTTP failure raises so the caller
    answers MODULE_LISTING_UNAVAILABLE.
    """
    trees: Dict[str, List[Dict]] = {}
    for root in PLUGIN_SET_ROOTS:
        entries: List[Dict] = []
        page = 1
        while True:
            response = requests.get(
                MODULE_PLUGINS_TREE_URL,
                params={'path': f'subprojects/{module}/{root}',
                        'per_page': MODULE_PLUGINS_PER_PAGE,
                        'page': page},
                timeout=MODULE_LISTING_TIMEOUT_SECONDS)
            if response.status_code == 404:
                # e.g. gst-plugins-ugly has no sys/ tree
                entries = []
                break
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                raise ModuleListingParseError(
                    f'unexpected GitLab tree response for {module}/{root}')
            entries.extend(batch)
            next_page = (response.headers.get('x-next-page') or '').strip()
            if not next_page:
                break
            page = int(next_page)
        trees[root] = entries
    return trees


def fetch_module_plugin_descriptions(module: str) -> Dict[str, str]:
    """
    {plugin_name: description} for one official module, fetched from
    the monorepo's raw docs/gst_plugins_cache.json (size-capped).
    Descriptions are an enhancement: any failure — HTTP error, timeout,
    oversized file, malformed JSON — returns {} and never raises, so
    the plugin listing proceeds without descriptions.
    """
    try:
        response = requests.get(
            MODULE_PLUGIN_DESCRIPTIONS_URL_TEMPLATE.format(module=module),
            timeout=MODULE_LISTING_TIMEOUT_SECONDS, stream=True)
        response.raise_for_status()
        chunks: List[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > MAX_PLUGIN_DESCRIPTION_CACHE_BYTES:
                logger.warning(
                    f'Plugin description cache for {module} exceeds '
                    f'{MAX_PLUGIN_DESCRIPTION_CACHE_BYTES} bytes; skipping')
                return {}
            chunks.append(chunk)
        return plugin_descriptions_from_cache(b''.join(chunks))
    except Exception as exc:
        logger.warning(
            f'Plugin description cache unavailable for {module}: {exc}')
        return {}


def module_plugins_cache_key(module: str) -> str:
    """ModuleIndexCache key of one module's plugin list."""
    return MODULE_PLUGINS_CACHE_KEY_PREFIX + module


def read_cached_module_plugins(module: str) -> Optional[Dict]:
    """The cached per-module plugin-list item, or None."""
    try:
        response = module_cache_table().get_item(
            Key={'cache_key': module_plugins_cache_key(module)})
    except Exception as exc:
        logger.warning(f'ModuleIndexCache read failed: {exc}')
        return None
    return response.get('Item')


def write_module_plugins_cache(module: str, plugins: List[Dict],
                               fetched_at_ms: int) -> None:
    """Cache one module's plugin list with fetchedAt and the 24-hour
    TTL (same pattern as the module index); a cache-write failure never
    fails the request that fetched the list."""
    try:
        module_cache_table().put_item(Item={
            'cache_key': module_plugins_cache_key(module),
            'module': module,
            'plugins': plugins,
            'fetchedAt': fetched_at_ms,
            'ttl': fetched_at_ms // 1000 + MODULE_INDEX_TTL_SECONDS,
        })
    except Exception as exc:
        logger.warning(f'ModuleIndexCache write failed: {exc}')


# --------------------------------------------------- fetch orchestration

def start_fetch(repo_url: str, revision: Optional[str], dest_prefix: str,
                usecase_id: str, plugin_id: str, version: int,
                revision_slug_id: Optional[str] = None) -> str:
    """
    StartBuild on the lightweight CodeBuild fetch step (4.1): clone
    `repo_url` at `revision` (default branch when empty) and sync the
    tree to s3://{bucket}/{dest_prefix}/. Never polls — the request
    path answers 202 immediately (API Gateway caps REST integrations at
    29 s) and the EventBridge-delivered build state change reaches
    `handle_fetch_result`, attributed back to the Plugin_Record by the
    PLUGIN_ID / PLUGIN_VERSION / USECASE_ID env overrides. Fetches of a
    multi-revision import (arch_revisions) additionally carry the
    REVISION_SLUG override so the result handler can update the right
    entry of the record's `fetches` map.

    Returns the CodeBuild build id.
    """
    env_overrides = [
        {'name': 'REPO_URL', 'value': repo_url, 'type': 'PLAINTEXT'},
        {'name': 'REVISION', 'value': revision or '', 'type': 'PLAINTEXT'},
        {'name': 'DEST_PREFIX', 'value': dest_prefix, 'type': 'PLAINTEXT'},
        {'name': 'USECASE_ID', 'value': usecase_id, 'type': 'PLAINTEXT'},
        {'name': 'PLUGIN_ID', 'value': plugin_id, 'type': 'PLAINTEXT'},
        {'name': 'PLUGIN_VERSION', 'value': str(version), 'type': 'PLAINTEXT'},
    ]
    if revision_slug_id:
        env_overrides.append(
            {'name': 'REVISION_SLUG', 'value': revision_slug_id,
             'type': 'PLAINTEXT'})
    start = codebuild.start_build(
        projectName=FETCH_PROJECT_NAME,
        environmentVariablesOverride=env_overrides,
    )
    return start['build']['id']


def fetch_build_id_from_arn(build_arn: str) -> str:
    """Extract 'project:uuid' from the EventBridge detail build-id ARN
    (mirrors plugin_builds.build_id_from_arn; duplicated locally so
    neither module imports the other in a cycle)."""
    if ':build/' in build_arn:
        return build_arn.split(':build/', 1)[1]
    return build_arn


def fetch_env_vars(detail: Dict) -> Dict[str, str]:
    """Environment variables of the finished fetch build (the StartBuild
    overrides, echoed back in the EventBridge detail)."""
    env = ((detail.get('additional-information') or {})
           .get('environment') or {}).get('environment-variables') or []
    return {var.get('name'): var.get('value') for var in env
            if isinstance(var, dict) and var.get('name')}


def list_source_tree(prefix: str) -> Dict[str, Optional[str]]:
    """
    File mapping {relative_path: content-or-None} of the synced source
    tree under the S3 prefix. Content is fetched (size-capped) only for
    the build-definition files the buildability scan consults and for
    gst_plugins_cache.json plugin-metadata files (per-plugin
    descriptions — a fetch failure there is non-fatal: the entry stays
    content-less and enumeration proceeds without descriptions).
    """
    files: Dict[str, Optional[str]] = {}
    kwargs = {'Bucket': PORTAL_ARTIFACTS_BUCKET, 'Prefix': prefix}
    while True:
        response = s3.list_objects_v2(**kwargs)
        for obj in response.get('Contents', []):
            relative = obj['Key'][len(prefix):]
            if not relative:
                continue
            files[relative] = None
        if not response.get('IsTruncated'):
            break
        kwargs['ContinuationToken'] = response['NextContinuationToken']

    for relative in files:
        name = posixpath.basename(relative)
        if name in BUILD_DEFINITION_FILES:
            obj = s3.get_object(
                Bucket=PORTAL_ARTIFACTS_BUCKET, Key=prefix + relative,
                Range=f'bytes=0-{MAX_BUILD_DEFINITION_BYTES - 1}')
            files[relative] = obj['Body'].read().decode('utf-8', errors='replace')
        elif name == PLUGIN_DOCS_CACHE_FILENAME:
            try:
                obj = s3.get_object(
                    Bucket=PORTAL_ARTIFACTS_BUCKET, Key=prefix + relative,
                    Range=f'bytes=0-{MAX_PLUGIN_DESCRIPTION_CACHE_BYTES - 1}')
                files[relative] = obj['Body'].read().decode(
                    'utf-8', errors='replace')
            except Exception as exc:
                logger.warning(
                    f'Could not read {relative} for plugin descriptions: '
                    f'{exc}')

    return files


def submit_builds(architectures: List[str]) -> Dict[str, Dict]:
    """
    Submit the imported source to the Plugin_Build_Service for the
    user-selected Target_Architectures (4.3): per-arch artifact entries
    queued on the Plugin_Record. Once the record is persisted as
    'imported', _start_queued_builds hands the queued entries to
    plugin_builds.start_queued_builds, which StartBuilds the per-arch
    CodeBuild projects (queued -> building); the EventBridge build
    results then record {s3Key, checksum, signature, buildStatus,
    logTail}.
    """
    return {arch: {'buildStatus': BUILD_QUEUED} for arch in architectures}


# ------------------------------- pure: platform requirements check
#
# Advisory per-platform requirements/compatibility check at import time.
# Users import sources (e.g. gst-plugins-good main, which requires
# GStreamer >= 1.24) that cannot build on the older JetPack platforms
# and only find out through an obscure meson subproject error at build
# time — nothing tells them WHY, so failed builds get retried
# repeatedly. The check parses the minimum GStreamer version the source
# requires from its root build definition (whose content
# list_source_tree already fetches) and compares it against the
# GStreamer version each Target_Architecture's build platform ships.
# It NEVER blocks or fails an import or a build (the user may know
# better), and every parsing failure degrades to "no requirement
# determined" = compatible everywhere.

#: GStreamer version each Target_Architecture's build platform ships,
#: from the dda-plugin-build images
#: (edge-cv-portal/plugin-build-images/Dockerfile.<arch>):
#:   - x86_64:        Ubuntu 22.04                        -> GStreamer 1.20
#:   - x86_64_nvidia: CUDA on Ubuntu 22.04                -> GStreamer 1.20
#:   - arm64_jp4:     L4T r32 (JetPack 4, Ubuntu 18.04)   -> GStreamer 1.14
#:   - arm64_jp5:     L4T r35 (JetPack 5, Ubuntu 20.04)   -> GStreamer 1.16
#:   - arm64_jp6:     L4T r36 (JetPack 6, Ubuntu 22.04)   -> GStreamer 1.20
PLATFORM_GSTREAMER_VERSIONS = {
    'x86_64': '1.20',
    'x86_64_nvidia': '1.20',
    'arm64_jp4': '1.14',
    'arm64_jp5': '1.16',
    'arm64_jp6': '1.20',
}

#: Platforms whose build image toolchain (Ubuntu 22.04: modern meson,
#: glib, and headers) can build a newer GStreamer via meson's
#: subproject fallback when the source requires more than the platform
#: ships. Observed in production: gst-plugins-good main (requires
#: GStreamer >= 1.24) builds fine on x86_64 / x86_64_nvidia /
#: arm64_jp6 (which ship 1.20) via the fallback, while arm64_jp4
#: (Ubuntu 18.04) and arm64_jp5 (Ubuntu 20.04) fail with an obscure
#: meson subproject error — their toolchains are too old to build a
#: current GStreamer from source.
PLATFORMS_WITH_SUBPROJECT_FALLBACK = frozenset(
    {'x86_64', 'x86_64_nvidia', 'arm64_jp6'})

#: Human-readable platform names for compatibility reasons (kept in
#: line with the frontend's ARCHITECTURE_LABELS in
#: frontend/src/pages/node-designer/types.ts).
PLATFORM_LABELS = {
    'x86_64': 'x86_64',
    'x86_64_nvidia': 'x86_64 (NVIDIA GPU)',
    'arm64_jp4': 'arm64 JetPack 4',
    'arm64_jp5': 'arm64 JetPack 5',
    'arm64_jp6': 'arm64 JetPack 6',
}

# meson: gst_req = '>= 1.24' / gst_req = '>= 1.24.0' (literal form,
# used by standalone plugin repositories).
_MESON_GST_REQ_LITERAL_RE = re.compile(
    r"""\bgst_req\s*=\s*['"]\s*>=\s*(\d+\.\d+(?:\.\d+)?)\s*['"]""")
# meson: gst_req = '>= @0@.@1@.0'.format(gst_version_major,
# gst_version_minor) — the form every gst-plugins-good/bad/ugly
# meson.build uses (main and the 1.x release branches alike): the
# requirement is the project's own major.minor series.
_MESON_GST_REQ_FORMAT_RE = re.compile(
    r"""\bgst_req\s*=\s*['"]\s*>=\s*@0@\.@1@[^'"]*['"]\s*\.\s*format\s*\(""")
# meson: the project version the format form derives major/minor from,
# e.g. project('gst-plugins-good', 'c', version : '1.24.2', ...). The
# first `version :` in a gst module meson.build is the project's
# (meson_version has no word boundary before 'version').
_MESON_PROJECT_VERSION_RE = re.compile(
    r"""\bversion\s*:\s*['"](\d+\.\d+(?:[0-9.]*))['"]""")
# meson: dependency('gstreamer-1.0', version : '>= 1.20', ...) with an
# inline literal version constraint.
_MESON_GST_DEP_VERSION_RE = re.compile(
    r"""\bdependency\s*\(\s*['"]gstreamer-1\.0['"][^)]*?"""
    r"""version\s*:\s*['"]\s*>=\s*(\d+\.\d+(?:\.\d+)?)""",
    re.DOTALL)
# autotools: GST_REQUIRED=1.16 / GST_REQUIRED_VERSION=1.16 /
# AC_SUBST(GST_REQUIRED, 1.16) assignments in configure.ac.
_AUTOTOOLS_GST_REQUIRED_RE = re.compile(
    r"""\bGST_REQUIRED\w*\s*[=,]\s*\[?\s*(\d+\.\d+(?:\.\d+)?)""")
# autotools: PKG_CHECK_MODULES(GST, gstreamer-1.0 >= 1.20) with an
# inline literal version constraint.
_AUTOTOOLS_GST_PKG_RE = re.compile(
    r"""gstreamer-1\.0\s*>=\s*(\d+\.\d+(?:\.\d+)?)""")


def _minor_version(version: Optional[str]) -> Optional[Tuple[int, int]]:
    """(major, minor) of a dotted version string, or None when it does
    not parse. Comparison happens at minor precision: the platform
    table carries no micro versions and GStreamer features land per
    minor release series."""
    if not version or not isinstance(version, str):
        return None
    match = re.match(r'\s*(\d+)\.(\d+)', version)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def gstreamer_requirement(
        files: Mapping[str, Optional[str]]) -> Optional[str]:
    """
    The minimum GStreamer version the source requires, parsed from the
    root build definition content (which list_source_tree already
    fetches for the buildability scan). Pure over the source-tree file
    mapping.

    Handled patterns:
      - meson ``gst_req = '>= 1.x[.y]'`` literal;
      - meson ``gst_req = '>= @0@.@1@.0'.format(gst_version_major,
        gst_version_minor)`` (the gst-plugins-good/bad/ugly form, main
        and release branches alike) resolved against the ``project(...
        version : '1.x.y')`` literal — the requirement is the project's
        own major.minor series;
      - meson ``dependency('gstreamer-1.0', version : '>= 1.x')``;
      - autotools ``GST_REQUIRED=1.x`` / ``AC_SUBST(GST_REQUIRED, 1.x)``
        and ``PKG_CHECK_MODULES(..., gstreamer-1.0 >= 1.x)`` in
        configure.ac / configure.in.

    Returns the version string (e.g. '1.24.0'), or None when no
    requirement can be determined — absence of a requirement is treated
    as compatible everywhere, and every parsing failure degrades to
    None (the check is advisory, never blocking).
    """
    meson = files.get('meson.build')
    if meson:
        match = _MESON_GST_REQ_LITERAL_RE.search(meson)
        if match:
            return match.group(1)
        if _MESON_GST_REQ_FORMAT_RE.search(meson):
            project_version = _MESON_PROJECT_VERSION_RE.search(meson)
            minor = _minor_version(
                project_version.group(1) if project_version else None)
            if minor:
                return f'{minor[0]}.{minor[1]}.0'
        match = _MESON_GST_DEP_VERSION_RE.search(meson)
        if match:
            return match.group(1)
    for name in ('configure.ac', 'configure.in'):
        content = files.get(name)
        if not content:
            continue
        match = _AUTOTOOLS_GST_REQUIRED_RE.search(content)
        if match:
            return match.group(1)
        match = _AUTOTOOLS_GST_PKG_RE.search(content)
        if match:
            return match.group(1)
    return None


def platform_compatibility(required_version: Optional[str],
                           architectures: List[str],
                           classification_or_module: Optional[str] = None
                           ) -> Dict[str, Dict]:
    """
    Per-platform requirements check for the requested
    Target_Architectures. Advisory only: incompatible architectures
    still queue builds — the user may know better.

    Returns {arch: {compatible, platformVersion, requiredVersion,
    reason, suggestedRevision}} where:
      - compatible is False exactly when both the requirement and the
        platform's GStreamer version are known, the platform's minor
        release series is older than required, and the platform's
        toolchain cannot build a newer GStreamer via meson's subproject
        fallback (PLATFORMS_WITH_SUBPROJECT_FALLBACK — the Ubuntu 22.04
        platforms satisfy newer requirements that way in production);
        anything unknown counts as compatible;
      - reason is a plain-language explanation on incompatible entries
        (e.g. "The source requires GStreamer >= 1.24; arm64 JetPack 5
        provides 1.16"), None otherwise;
      - suggestedRevision is, for official GStreamer modules only
        (`classification_or_module` carries the provenance moduleName
        or a good/bad/ugly classification), the upstream release branch
        matching the platform's GStreamer minor (e.g. '1.16' for
        arm64_jp5, '1.14' for arm64_jp4) — verified working in
        production. Non-official repositories get no suggestion (None):
        their branch layout is unknown.
    """
    required = _minor_version(required_version)
    official = bool(classification_or_module) and (
        classification_or_module != 'unclassified')
    result: Dict[str, Dict] = {}
    for arch in architectures:
        platform_version = PLATFORM_GSTREAMER_VERSIONS.get(arch)
        entry: Dict = {
            'compatible': True,
            'platformVersion': platform_version,
            'requiredVersion': required_version,
            'reason': None,
            'suggestedRevision': None,
        }
        platform = _minor_version(platform_version)
        if (required and platform and platform < required
                and arch not in PLATFORMS_WITH_SUBPROJECT_FALLBACK):
            label = PLATFORM_LABELS.get(arch, arch)
            entry['compatible'] = False
            entry['reason'] = (
                f'The source requires GStreamer >= {required_version}; '
                f'{label} provides {platform_version}')
            if official:
                entry['suggestedRevision'] = platform_version
        result[arch] = entry
    return result


def evaluate_fetched_tree(files: Mapping[str, Optional[str]], name: str,
                          selected_plugins: List[str],
                          architectures: List[str],
                          classification_or_module: Optional[str] = None
                          ) -> Tuple[Dict, Dict]:
    """
    Post-fetch import decision, pure over the synced source-tree file
    mapping (shared by the fetch-result handler; formerly inlined in
    the synchronous import path).

    Returns (scan, updates) where `updates` are the Plugin_Record
    fields to set:
      - unbuildable tree: import_status failed + import_finding (4.5);
      - buildable with an import-time selection recorded: selection on
        the record and provenance, import_status imported, builds
        queued (4.3);
      - buildable plugin set (> 1 enumerated plugin, no selection):
        import_status pending_selection with plugins_found — build
        submission defers to the select-plugins endpoint;
      - buildable single plugin: import_status imported, builds queued.

    Every outcome additionally carries the advisory
    `platform_compatibility` map (gstreamer_requirement +
    platform_compatibility over the requested Target_Architectures):
    it informs the user per platform whether the source can work and
    which revision would, but never blocks — incompatible architectures
    still queue builds.
    """
    scan = scan_buildability(files)
    plugins_found = enumerate_plugins(files, single_plugin_name=name)
    compatibility = platform_compatibility(
        gstreamer_requirement(files), architectures,
        classification_or_module)

    if not scan['buildable']:
        return scan, {
            'import_status': IMPORT_STATUS_FAILED,
            'import_finding': scan['finding'],
            'platform_compatibility': compatibility,
        }

    updates: Dict = {'plugins_found': plugins_found,
                     'platform_compatibility': compatibility}
    if selected_plugins:
        # The user already picked the subset to import (from the module
        # plugin list at import time): record it and queue builds now —
        # the pending-selection step is skipped.
        updates['selected_plugins'] = selected_plugins
        updates['provenance.selectedPlugins'] = selected_plugins
        updates['import_status'] = IMPORT_STATUS_IMPORTED
        updates['artifacts'] = submit_builds(architectures)
    elif len(plugins_found) > 1:
        # Plugin-set import: the user selects which individual plugins
        # to import before builds are submitted (the select-plugins
        # endpoint advances the record).
        updates['import_status'] = IMPORT_STATUS_PENDING_SELECTION
    else:
        # Single-plugin repository: selection skipped; queue builds for
        # the selected Target_Architectures (4.3).
        updates['import_status'] = IMPORT_STATUS_IMPORTED
        updates['artifacts'] = submit_builds(architectures)
    return scan, updates


# ----------------------------------------------------------------- handlers

def import_detail(item: Dict) -> Dict:
    """version_detail plus the import-specific fields (import status,
    finding, enumerated plugins, and the recorded selection)."""
    detail = version_detail(item)
    detail['import_status'] = item.get('import_status')
    if item.get('import_finding'):
        detail['import_finding'] = item['import_finding']
    if item.get('plugins_found') is not None:
        detail['plugins_found'] = item['plugins_found']
    if item.get('selected_plugins') is not None:
        detail['selected_plugins'] = item['selected_plugins']
    if item.get('platform_compatibility') is not None:
        detail['platform_compatibility'] = item['platform_compatibility']
    return detail


def import_repository(event: Dict, user: Dict) -> Dict:
    """
    POST /plugins/import
    Body: {usecase_id, repo_url, revision?, architectures, name?,
           description?, deepstream?, module_name?, selected_plugins?}

    Asynchronous import: validates the request, creates the
    Plugin_Record immediately (lifecycle dev, review pending) with
    import provenance + classification (4.2, 15.5) and import_status
    'fetching', StartBuilds the fetch project WITHOUT polling, and
    answers 202 with the record. The fetch outcome arrives via
    EventBridge at `handle_fetch_result`, which scans buildability and
    advances the record: failed with the finding (4.5), imported with
    builds queued (4.3), or pending_selection for plugin sets
    (deferring build submission to
    POST /plugins/{id}/versions/{v}/select-plugins). Clients poll
    GET /plugins/{id}/versions/{v} until import_status leaves
    'fetching'.
    """
    body, err = parse_body(event)
    if err:
        return err

    usecase_id = body.get('usecase_id')
    repo_url = body.get('repo_url')
    revision = body.get('revision') or None
    architectures = body.get('architectures')
    module_name = body.get('module_name') or None
    deepstream = bool(body.get('deepstream', False))

    missing = [f for f in ('usecase_id', 'repo_url') if not body.get(f)]
    if missing:
        return error_response(400, 'MISSING_FIELDS',
                              f"Missing required fields: {', '.join(missing)}")

    url_error = validate_repo_url(repo_url)
    if url_error:
        return error_response(400, 'INVALID_REPO_URL', url_error,
                              {'repo_url': repo_url})

    if revision is not None and not isinstance(revision, str):
        return error_response(400, 'INVALID_REVISION',
                              'revision must be a string')

    if (not isinstance(architectures, list) or not architectures
            or not all(isinstance(a, str) for a in architectures)):
        return error_response(400, 'INVALID_ARCHITECTURES',
                              'architectures must be a non-empty list of '
                              'Target_Architecture identifiers')
    architectures = sorted(set(architectures))
    invalid = [a for a in architectures if a not in DEVICE_ARCHITECTURES]
    if invalid:
        return error_response(400, 'INVALID_ARCHITECTURES',
                              f"Unknown Target_Architectures: {', '.join(invalid)}",
                              {'valid': list(DEVICE_ARCHITECTURES)})
    if deepstream:
        # DeepStream targets Jetson: selectable architectures restricted
        # to the JetPack builds (Requirement 5.1).
        non_jetson = [a for a in architectures
                      if a not in DEEPSTREAM_ARCHITECTURES]
        if non_jetson:
            return error_response(
                400, 'INVALID_ARCHITECTURES',
                'DeepStream imports may only target: '
                f"{', '.join(DEEPSTREAM_ARCHITECTURES)}",
                {'invalid': non_jetson})

    # Optional per-architecture revision overrides: each arch's
    # effective revision is arch_revisions[arch] or the top-level
    # revision (default branch when neither is given). Absent = one
    # revision everywhere, today's behavior exactly.
    arch_revisions = body.get('arch_revisions')
    arch_rev_error = validate_arch_revisions(arch_revisions, architectures)
    if arch_rev_error:
        return error_response(400, 'INVALID_ARCH_REVISIONS', arch_rev_error,
                              {'architectures': architectures})

    # Optional import-time plugin selection (from the module plugin
    # list, GET /plugin-modules?module=...). Absent or empty = whole
    # module, today's behavior.
    selection_error = validate_import_selected_plugins(
        body.get('selected_plugins'))
    if selection_error:
        return error_response(400, 'INVALID_PLUGIN_SELECTION',
                              selection_error)
    selected_plugins = dedupe_selected_plugins(body.get('selected_plugins'))

    if not can_manage(user, usecase_id, Permission.NODE_DESIGNER_IMPORT):
        return forbidden_response(user, event, usecase_id,
                                  Permission.NODE_DESIGNER_IMPORT)

    try:
        get_usecase(usecase_id)
    except ValueError:
        return error_response(404, 'USECASE_NOT_FOUND', 'Use case not found')

    # Record name: explicit `name` wins; otherwise the URL-derived base
    # name, with a single-plugin import-time selection appended
    # ("gst-plugins-good-rtsp") so the record shows what was imported.
    name = derive_import_name(body.get('name'), repo_url, selected_plugins)
    plugin_id = str(uuid.uuid4())
    version = 1
    prefix = source_s3_prefix(usecase_id, plugin_id, version)

    # Effective per-arch revision plan: a single distinct revision keeps
    # today's one-fetch flat layout (mode 'single' also collapses
    # arch_revisions that all name the same revision); multiple distinct
    # revisions fetch once each into rev-{slug}/ prefixes (mode 'multi').
    plan = revision_fetch_plan(revision, arch_revisions, architectures,
                               prefix)
    fetches: Optional[Dict[str, Dict]] = None
    fetch_build_id: Optional[str] = None

    # --- fetch step: StartBuild only, never polled in the request path.
    # API Gateway caps REST integrations at 29 s; slow clones (e.g.
    # gst-plugins-good) settle via the EventBridge result instead.
    try:
        if plan['mode'] == 'single':
            revision = plan['revision']
            fetch_build_id = start_fetch(
                repo_url, revision, prefix.rstrip('/'),
                usecase_id=usecase_id, plugin_id=plugin_id, version=version)
        else:
            fetches = plan['fetches']
            for slug in sorted(fetches):
                entry = fetches[slug]
                entry['fetch_build_id'] = start_fetch(
                    repo_url,
                    (None if entry['revision'] == DEFAULT_REVISION
                     else entry['revision']),
                    entry['source_prefix'].rstrip('/'),
                    usecase_id=usecase_id, plugin_id=plugin_id,
                    version=version, revision_slug_id=slug)
    except Exception as exc:
        logger.error(f'Fetch StartBuild failed: {exc}', exc_info=True)
        log_audit_event(
            user_id=user['user_id'],
            action='import_plugin_record',
            resource_type='plugin_record',
            resource_id=plugin_id,
            result='failure',
            details={'usecase_id': usecase_id, 'repo_url': repo_url,
                     'revision': revision or DEFAULT_REVISION,
                     'reason': 'REPO_FETCH_FAILED'}
        )
        return error_response(
            502, 'REPO_FETCH_FAILED',
            'The repository fetch could not be started',
            {'repo_url': repo_url, 'revision': revision or DEFAULT_REVISION})

    # --- Plugin_Record creation (4.2, 15.5): import_status 'fetching',
    # provenance recorded now, plugins_found only once the fetch settles.
    timestamp = now_ms()
    provenance = import_provenance(repo_url, revision, module_name,
                                   user['user_id'], timestamp)
    item = new_version_item(
        plugin_id=plugin_id, version=version, usecase_id=usecase_id,
        name=name, kind='imported', user_id=user['user_id'],
        timestamp=timestamp, description=body.get('description', ''),
        deepstream=deepstream, provenance=provenance,
    )
    item['requested_architectures'] = architectures
    item['import_status'] = IMPORT_STATUS_FETCHING
    if plan['mode'] == 'single':
        item['fetch_build_id'] = fetch_build_id
    else:
        # Multi-revision import: per-revision fetch map + arch->slug
        # mapping. The record's source_s3_prefix points at the DEFAULT
        # revision's tree so the buildability scan, plugin enumeration,
        # source inspection, and node-generator acceptance all read one
        # deterministic tree; per-arch builds resolve their own tree
        # via arch_revisions -> fetches[slug].source_prefix.
        item['fetches'] = fetches
        item['arch_revisions'] = plan['arch_revisions']
        item['default_fetch_slug'] = plan['default_slug']
        item['source_s3_prefix'] = \
            fetches[plan['default_slug']]['source_prefix']
    if selected_plugins:
        # Import-time selection, applied by handle_fetch_result once
        # the tree proves buildable (then recorded as selected_plugins
        # + provenance.selectedPlugins so plugin_builds.py passes it to
        # CodeBuild as SELECTED_PLUGINS); removed when the fetch settles.
        item['pending_selected_plugins'] = selected_plugins

    plugin_table().put_item(
        Item=item,
        ConditionExpression='attribute_not_exists(plugin_id)'
    )

    log_audit_event(
        user_id=user['user_id'],
        action='import_plugin_record',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': usecase_id, 'repo_url': repo_url,
                 'revision': revision or DEFAULT_REVISION,
                 'classification': provenance['classification'],
                 'architectures': architectures,
                 'import_status': IMPORT_STATUS_FETCHING,
                 **({'fetch_build_id': fetch_build_id}
                    if plan['mode'] == 'single' else
                    {'arch_revisions': plan['arch_revisions'],
                     'fetch_build_ids': {
                         slug: fetches[slug]['fetch_build_id']
                         for slug in fetches}}),
                 **({'selected_plugins': selected_plugins}
                    if selected_plugins else {})}
    )

    return create_response(202, {
        'plugin': import_detail(item),
        'import': {
            'status': IMPORT_STATUS_FETCHING,
            **({'buildId': fetch_build_id}
               if plan['mode'] == 'single' else
               {'fetchBuildIds': {slug: fetches[slug]['fetch_build_id']
                                  for slug in fetches}}),
        },
    })


def _advance_import_record(plugin_id: str, version: int,
                           updates: Dict) -> bool:
    """
    Apply the fetch outcome to the Plugin_Record, guarded on
    import_status still being 'fetching' so a duplicate EventBridge
    delivery never re-applies (idempotency, mirroring
    handle_build_result's guards). The transient
    pending_selected_plugins field is removed once the fetch settles.

    Returns False when the record already left 'fetching'.
    """
    names: Dict[str, str] = {}
    values: Dict = {':fetching': IMPORT_STATUS_FETCHING, ':t': now_ms()}
    sets = ['updated_at = :t']
    for index, (field, value) in enumerate(sorted(updates.items())):
        placeholder = f':v{index}'
        values[placeholder] = value
        if field == 'provenance.selectedPlugins':
            sets.append(f'provenance.selectedPlugins = {placeholder}')
        else:
            names[f'#f{index}'] = field
            sets.append(f'#f{index} = {placeholder}')
    kwargs = dict(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression=('SET ' + ', '.join(sets)
                          + ' REMOVE pending_selected_plugins'),
        ConditionExpression='import_status = :fetching',
        ExpressionAttributeValues=values,
    )
    if names:
        kwargs['ExpressionAttributeNames'] = names
    try:
        plugin_table().update_item(**kwargs)
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            return False
        raise
    return True


def _start_queued_builds(plugin_id: str, version: int) -> None:
    """
    Auto-start the per-arch CodeBuild builds an import just queued
    (import_status advanced to 'imported'): delegates to
    plugin_builds.start_queued_builds, which flips the queued artifact
    entries to building with their build ids. plugin_builds is imported
    lazily — it already lazily imports this module for fetch results,
    so a module-level import would be circular. Mirroring the
    trigger_component_packaging pattern, an auto-start failure never
    fails the fetch-result handler (start_queued_builds itself never
    raises; this guard additionally covers the import).
    """
    try:
        import plugin_builds
        plugin_builds.start_queued_builds(plugin_id, version)
    except Exception as e:
        logger.warning(f"Auto-start of queued builds for {plugin_id} "
                       f"v{version} failed: {e}")


def handle_fetch_result(detail: Dict) -> Dict:
    """
    EventBridge CodeBuild Build State Change handler for the fetch
    project (delegated from plugin_builds.py's handler — same rule
    'dda-portal-plugin-build-results', same Lambda bundle). Idempotent
    on the fetch build id.

    - SUCCEEDED: scans the synced tree (list_source_tree +
      scan_buildability + enumerate_plugins, exactly as the old
      synchronous path) and advances the record via
      `evaluate_fetched_tree`: failed (finding, 4.5), pending_selection
      (plugin set without a recorded selection), or imported with
      builds queued and immediately started (4.3, _start_queued_builds).
    - FAILED / FAULT / STOPPED / TIMED_OUT: marks the record failed
      with the REPO_FETCH_FAILED finding (the record exists so the UI
      can show why — a deliberate change from the old synchronous
      no-record behavior, 4.4).

    Post-import revision adjustments (adjust_revision) fetch against a
    SETTLED record: their results — a `fetches` entry named by
    REVISION_SLUG carrying the pending_archs marker — route to
    _handle_adjustment_fetch_result before the import paths above.
    """
    build_id = fetch_build_id_from_arn(detail.get('build-id') or '')
    build_status = detail.get('build-status')
    env = fetch_env_vars(detail)
    plugin_id = env.get('PLUGIN_ID')
    version_raw = env.get('PLUGIN_VERSION')
    if not plugin_id or not version_raw:
        logger.warning(f"Fetch build {build_id} carries no "
                       "PLUGIN_ID/PLUGIN_VERSION; skipping")
        return {'recorded': False, 'reason': 'missing fetch metadata'}
    version = int(version_raw)

    item = get_version_item(plugin_id, version)
    if not item:
        logger.warning(f"Fetch build {build_id}: Plugin_Record "
                       f"{plugin_id} v{version} not found")
        return {'recorded': False, 'reason': 'plugin record not found'}

    fetches = item.get('fetches') or {}
    adjustment_slug = env.get('REVISION_SLUG')
    if (adjustment_slug
            and (fetches.get(adjustment_slug) or {}).get('pending_archs')):
        # Post-import revision adjustment fetch (adjust_revision): the
        # slug's entry carries the pending_archs marker — a settled
        # record, not an import in flight, so it must be routed before
        # the import_status == 'fetching' paths below
        # (imported-plugin-revision-adjustment-fix, 2.3/2.4).
        return _handle_adjustment_fetch_result(item, build_id,
                                               build_status, env)

    if fetches:
        # Multi-revision import (arch_revisions): one fetch per distinct
        # revision, each attributed by its REVISION_SLUG env override.
        return _handle_multi_fetch_result(item, build_id, build_status, env)

    # Idempotency on the fetch build id: skip events from superseded
    # builds and duplicate deliveries of an already-settled result.
    recorded_build_id = item.get('fetch_build_id')
    if recorded_build_id and recorded_build_id != build_id:
        logger.info(f"Fetch build {build_id} superseded by "
                    f"{recorded_build_id}; skipping")
        return {'recorded': False, 'reason': 'superseded build'}
    if item.get('import_status') != IMPORT_STATUS_FETCHING:
        logger.info(f"Fetch build {build_id} already recorded; "
                    "skipping duplicate delivery")
        return {'recorded': False, 'reason': 'already recorded'}

    architectures = [str(a) for a in item.get('requested_architectures') or []]
    pending_selection = [str(n) for n
                         in item.get('pending_selected_plugins') or []]

    if build_status == 'SUCCEEDED':
        files = list_source_tree(item.get('source_s3_prefix') or '')
        record_provenance = item.get('provenance') or {}
        scan, updates = evaluate_fetched_tree(
            files, item.get('name') or plugin_id,
            pending_selection, architectures,
            # Official-module signal for revision suggestions in the
            # advisory platform_compatibility map: the provenance
            # moduleName (module listing imports) or a good/bad/ugly
            # classification.
            classification_or_module=(record_provenance.get('moduleName')
                                      or record_provenance.get('classification')))
        buildable = scan['buildable']
    else:
        # Unreachable repository or missing revision (4.4).
        updates = {
            'import_status': IMPORT_STATUS_FAILED,
            'import_finding': FETCH_FAILURE_FINDING,
            'import_error_code': 'REPO_FETCH_FAILED',
        }
        buildable = False

    if not _advance_import_record(plugin_id, version, updates):
        return {'recorded': False, 'reason': 'already recorded'}

    if updates['import_status'] == IMPORT_STATUS_IMPORTED:
        # The import settled with builds queued: start them now (3.1).
        _start_queued_builds(plugin_id, version)

    provenance = item.get('provenance') or {}
    log_audit_event(
        user_id=(provenance.get('importedBy')
                 or item.get('created_by') or 'system'),
        action='complete_plugin_import',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success' if buildable else 'failure',
        details={'usecase_id': item.get('usecase_id'), 'version': version,
                 'fetch_build_id': build_id,
                 'fetch_status': build_status,
                 'import_status': updates['import_status'],
                 **({'reason': 'REPO_FETCH_FAILED'}
                    if build_status != 'SUCCEEDED' else {})}
    )
    logger.info(f"Recorded fetch {build_id} for {plugin_id} v{version}: "
                f"{updates['import_status']}")
    return {'recorded': True, 'import_status': updates['import_status']}


def _handle_multi_fetch_result(item: Dict, build_id: str,
                               build_status: Optional[str],
                               env: Dict[str, str]) -> Dict:
    """
    One fetch result of a multi-revision import (arch_revisions). The
    REVISION_SLUG env override names the entry of the record's
    `fetches` map this build synced; idempotency is guarded PER SLUG
    (fetch_build_id match + the slug's status still 'fetching'),
    mirroring the single-fetch guards. The slug settles to
    succeeded/failed and the record's import_status leaves 'fetching'
    only once EVERY fetch has settled:
      - any fetch failed -> import_status 'failed' with a finding
        naming the failing revision(s); the per-fetch statuses stay on
        the record so the UI can show which trees did sync;
      - all succeeded -> buildability scan + plugin enumeration run on
        the DEFAULT revision's tree (the record's source_s3_prefix) and
        evaluate_fetched_tree advances the record exactly like a
        single-revision import (imported / pending_selection / failed).
    """
    plugin_id = item['plugin_id']
    version = int(item['version'])
    fetches = item.get('fetches') or {}
    slug = env.get('REVISION_SLUG')
    if not slug or slug not in fetches:
        logger.warning(f"Fetch build {build_id} for {plugin_id} v{version} "
                       f"carries no known REVISION_SLUG ({slug!r}); skipping")
        return {'recorded': False, 'reason': 'missing fetch metadata'}

    entry = fetches[slug] or {}
    recorded_build_id = entry.get('fetch_build_id')
    if recorded_build_id and recorded_build_id != build_id:
        logger.info(f"Fetch build {build_id} superseded by "
                    f"{recorded_build_id}; skipping")
        return {'recorded': False, 'reason': 'superseded build'}
    if (entry.get('status') != FETCH_STATUS_FETCHING
            or item.get('import_status') != IMPORT_STATUS_FETCHING):
        logger.info(f"Fetch build {build_id} already recorded; "
                    "skipping duplicate delivery")
        return {'recorded': False, 'reason': 'already recorded'}

    settled = (FETCH_STATUS_SUCCEEDED if build_status == 'SUCCEEDED'
               else FETCH_STATUS_FAILED)
    # Per-slug conditional write: a duplicate delivery of the same
    # result loses the condition and changes nothing (idempotency).
    try:
        plugin_table().update_item(
            Key={'plugin_id': plugin_id, 'version': version},
            UpdateExpression='SET fetches.#s.#st = :settled, updated_at = :t',
            ConditionExpression=('fetches.#s.#st = :pending AND '
                                 'import_status = :fetching'),
            ExpressionAttributeNames={'#s': slug, '#st': 'status'},
            ExpressionAttributeValues={':settled': settled,
                                       ':pending': FETCH_STATUS_FETCHING,
                                       ':fetching': IMPORT_STATUS_FETCHING,
                                       ':t': now_ms()},
        )
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            return {'recorded': False, 'reason': 'already recorded'}
        raise

    # Settlement check over the just-written state (consistent read so
    # concurrent slug deliveries never all see an unsettled map).
    refreshed = plugin_table().get_item(
        Key={'plugin_id': plugin_id, 'version': version},
        ConsistentRead=True).get('Item') or {}
    fetches = refreshed.get('fetches') or {}
    statuses = {s: (e or {}).get('status') for s, e in fetches.items()}
    if any(status == FETCH_STATUS_FETCHING for status in statuses.values()):
        logger.info(f"Recorded fetch {build_id} ({slug}: {settled}) for "
                    f"{plugin_id} v{version}; other fetches still running")
        return {'recorded': True, 'import_status': IMPORT_STATUS_FETCHING,
                'revision_slug': slug, 'fetch_status': settled}

    failed_slugs = sorted(s for s, status in statuses.items()
                          if status != FETCH_STATUS_SUCCEEDED)
    architectures = [str(a) for a
                     in refreshed.get('requested_architectures') or []]
    pending_selection = [str(n) for n
                         in refreshed.get('pending_selected_plugins') or []]
    record_provenance = refreshed.get('provenance') or {}

    if failed_slugs:
        failed_revisions = [str((fetches[s] or {}).get('revision') or s)
                            for s in failed_slugs]
        updates = {
            'import_status': IMPORT_STATUS_FAILED,
            'import_finding': multi_fetch_failure_finding(failed_revisions),
            'import_error_code': 'REPO_FETCH_FAILED',
        }
        buildable = False
    else:
        files = list_source_tree(refreshed.get('source_s3_prefix') or '')
        scan, updates = evaluate_fetched_tree(
            files, refreshed.get('name') or plugin_id,
            pending_selection, architectures,
            classification_or_module=(record_provenance.get('moduleName')
                                      or record_provenance.get(
                                          'classification')))
        buildable = scan['buildable']

    if not _advance_import_record(plugin_id, version, updates):
        # A concurrent delivery finalized the record between the slug
        # write and this transition; the slug outcome itself is recorded.
        return {'recorded': True, 'revision_slug': slug,
                'import_status': (get_version_item(plugin_id, version)
                                  or {}).get('import_status')}

    if updates['import_status'] == IMPORT_STATUS_IMPORTED:
        # The import settled with builds queued: start them now (3.1).
        _start_queued_builds(plugin_id, version)

    log_audit_event(
        user_id=(record_provenance.get('importedBy')
                 or refreshed.get('created_by') or 'system'),
        action='complete_plugin_import',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success' if buildable else 'failure',
        details={'usecase_id': refreshed.get('usecase_id'),
                 'version': version,
                 'fetch_statuses': statuses,
                 'import_status': updates['import_status'],
                 **({'reason': 'REPO_FETCH_FAILED'}
                    if failed_slugs else {})}
    )
    logger.info(f"Recorded fetch {build_id} for {plugin_id} v{version}: "
                f"{updates['import_status']}")
    return {'recorded': True, 'import_status': updates['import_status']}


def _handle_adjustment_fetch_result(item: Dict, build_id: str,
                                    build_status: Optional[str],
                                    env: Dict[str, str]) -> Dict:
    """
    One fetch result of a post-import revision adjustment
    (adjust_revision, imported-plugin-revision-adjustment-fix): the
    REVISION_SLUG env override names the `fetches` entry whose
    pending_archs marker routed the delivery here. The record has
    already settled — import_status is never written on this path.

    Idempotency mirrors _handle_multi_fetch_result: one per-slug
    conditional write guarded on the entry's fetch_build_id matching
    this build AND its status still 'fetching', so superseded builds
    and duplicate deliveries change nothing.

    - SUCCEEDED: the entry settles 'succeeded' with pending_archs
      cleared, every pending arch maps through arch_revisions[arch] =
      slug (the map is created when the record is still flat), and the
      queued builds start — their source now resolves through the
      adjusted tree via plugin_builds.arch_source_prefix (2.3).
    - FAILED / FAULT / STOPPED / TIMED_OUT: the entry settles 'failed'
      with pending_archs cleared and each pending arch's artifact entry
      records the fetch-failure logTail — arch_revisions and every
      other architecture's entry are untouched, so the prior mapping
      and other platforms' builds stay intact (2.4, 3.5).

    Audit-logged as the record's created_by (no authenticated user on
    the EventBridge path). Never raises beyond what handle_build_result
    already tolerates (only unexpected DynamoDB errors propagate,
    exactly like _handle_multi_fetch_result).
    """
    plugin_id = item['plugin_id']
    version = int(item['version'])
    slug = env.get('REVISION_SLUG') or ''
    entry = (item.get('fetches') or {}).get(slug) or {}

    recorded_build_id = entry.get('fetch_build_id')
    if recorded_build_id and recorded_build_id != build_id:
        logger.info(f"Adjustment fetch build {build_id} superseded by "
                    f"{recorded_build_id}; skipping")
        return {'recorded': False, 'reason': 'superseded build'}
    if entry.get('status') != FETCH_STATUS_FETCHING:
        logger.info(f"Adjustment fetch build {build_id} already recorded; "
                    "skipping duplicate delivery")
        return {'recorded': False, 'reason': 'already recorded'}

    pending = [str(a) for a in entry.get('pending_archs') or []]
    revision = str(entry.get('revision') or slug)
    settled = (FETCH_STATUS_SUCCEEDED if build_status == 'SUCCEEDED'
               else FETCH_STATUS_FAILED)

    # One conditional write settles the slot AND applies the per-arch
    # outcome (arch_revisions mapping on success, fetch-failure artifact
    # entries on failure) atomically: a duplicate delivery loses the
    # condition and changes nothing.
    names: Dict[str, str] = {'#s': slug, '#st': 'status'}
    values: Dict = {':settled': settled, ':bid': build_id,
                    ':pending': FETCH_STATUS_FETCHING, ':t': now_ms()}
    sets = ['fetches.#s.#st = :settled', 'updated_at = :t']

    if settled == FETCH_STATUS_SUCCEEDED:
        # Flip the pending archs' mappings to the adjusted tree (2.3).
        if item.get('arch_revisions') is not None:
            values[':slug'] = slug
            for index, arch in enumerate(pending):
                names[f'#a{index}'] = arch
                sets.append(f'arch_revisions.#a{index} = :slug')
        else:
            values[':ar'] = {arch: slug for arch in pending}
            sets.append('arch_revisions = :ar')
    else:
        # The fetch failure surfaces on the pending archs' entries ONLY
        # — arch_revisions (the prior mapping) and every other
        # architecture's entry are untouched (2.4, 3.5).
        values[':fail'] = {
            'buildStatus': BUILD_FAILED,
            'logTail': adjustment_fetch_failure_log_tail(revision),
        }
        for index, arch in enumerate(pending):
            names[f'#a{index}'] = arch
            sets.append(f'artifacts.#a{index} = :fail')

    try:
        plugin_table().update_item(
            Key={'plugin_id': plugin_id, 'version': version},
            UpdateExpression=('SET ' + ', '.join(sets)
                              + ' REMOVE fetches.#s.pending_archs'),
            ConditionExpression=('fetches.#s.fetch_build_id = :bid AND '
                                 'fetches.#s.#st = :pending'),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if (e.response.get('Error', {}).get('Code')
                == 'ConditionalCheckFailedException'):
            return {'recorded': False, 'reason': 'already recorded'}
        raise

    if settled == FETCH_STATUS_SUCCEEDED:
        # The pending archs are queued with their mappings in place:
        # start their builds now (only queued entries are touched).
        # Auto-start failure never fails the fetch-result handler.
        _start_queued_builds(plugin_id, version)

    log_audit_event(
        user_id=item.get('created_by') or 'system',
        action='adjust_plugin_revision',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result=('success' if settled == FETCH_STATUS_SUCCEEDED
                else 'failure'),
        details={'usecase_id': item.get('usecase_id'), 'version': version,
                 'revision': revision, 'revision_slug': slug,
                 'architectures': pending,
                 'fetch_build_id': build_id,
                 'fetch_status': build_status,
                 **({'reason': 'REPO_FETCH_FAILED'}
                    if settled != FETCH_STATUS_SUCCEEDED else {})}
    )
    logger.info(f"Recorded adjustment fetch {build_id} ({slug}: {settled}) "
                f"for {plugin_id} v{version}; archs: {', '.join(pending)}")
    return {'recorded': True, 'revision_slug': slug,
            'fetch_status': settled,
            'import_status': item.get('import_status')}


def select_plugins(event: Dict, user: Dict, plugin_id: str,
                   version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/select-plugins
    Body: {selected_plugins: [name, ...]}

    Complete a plugin-set import awaiting selection: validates the
    selection is non-empty and a subset of the enumerated plugins_found,
    records selected_plugins on the Plugin_Record (and in provenance),
    advances the import status to imported, and submits builds for the
    previously requested Target_Architectures (queued and immediately
    started via plugin_builds.start_queued_builds). plugin_builds.py
    passes the recorded selection to CodeBuild as the PLUGIN_TARGETS env
    override so the build image builds only the selected plugins.
    """
    body, err = parse_body(event)
    if err:
        return err

    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()
    err = authorize_record_access(user, event, item, manage=True,
                                  permission=Permission.NODE_DESIGNER_IMPORT)
    if err:
        return err

    if item.get('import_status') != IMPORT_STATUS_PENDING_SELECTION:
        return error_response(
            409, 'SELECTION_NOT_PENDING',
            'This Plugin_Record version is not awaiting a plugin '
            'selection',
            {'import_status': item.get('import_status')})

    plugins_found = item.get('plugins_found') or []
    found_names = [entry.get('name') for entry in plugins_found]
    selection_error = validate_plugin_selection(
        body.get('selected_plugins'), found_names)
    if selection_error:
        return error_response(400, 'INVALID_PLUGIN_SELECTION',
                              selection_error,
                              {'plugins_found': found_names})

    # De-duplicated, order-normalized selection; original enumeration
    # order is preserved for display.
    selected_set = set(body['selected_plugins'])
    selected = [name for name in found_names if name in selected_set]

    architectures = [str(a) for a in item.get('requested_architectures') or []]
    artifacts = submit_builds(architectures)

    # A single-plugin selection renames the record ("{name}-{plugin}")
    # while the name is still the URL-derived default, so the library
    # and detail views show which plugin was imported.
    new_name = selection_rename(
        item.get('name'), (item.get('provenance') or {}).get('repoUrl'),
        selected)

    update_expression = ('SET selected_plugins = :sp, '
                         'provenance.selectedPlugins = :sp, '
                         'artifacts = :a, import_status = :st, '
                         'updated_at = :t')
    expression_values = {
        ':sp': selected,
        ':a': artifacts,
        ':st': IMPORT_STATUS_IMPORTED,
        ':t': now_ms(),
        ':pending': IMPORT_STATUS_PENDING_SELECTION,
    }
    update_kwargs = dict(
        Key={'plugin_id': plugin_id, 'version': version},
        ConditionExpression='import_status = :pending',
    )
    if new_name:
        update_expression += ', #n = :n'  # 'name' is a reserved word
        expression_values[':n'] = new_name
        update_kwargs['ExpressionAttributeNames'] = {'#n': 'name'}
    plugin_table().update_item(
        UpdateExpression=update_expression,
        ExpressionAttributeValues=expression_values,
        **update_kwargs,
    )

    # The selection settled the import with builds queued: start them
    # now (3.1); auto-start failure never fails the selection.
    _start_queued_builds(plugin_id, version)

    log_audit_event(
        user_id=user['user_id'],
        action='select_import_plugins',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'selected_plugins': selected,
                 'architectures': architectures,
                 **({'renamed_to': new_name} if new_name else {})}
    )

    updated = get_version_item(plugin_id, version)
    return create_response(200, {
        'plugin': import_detail(updated),
        'import': {
            'status': IMPORT_STATUS_IMPORTED,
            'selected_plugins': selected,
            'submitted_architectures': architectures,
        },
    })


def adjust_revision(event: Dict, user: Dict, plugin_id: str,
                    version: int) -> Dict:
    """
    POST /plugins/{id}/versions/{v}/adjust-revision
    Body: {architecture, revision}

    Apply a per-platform source-revision override to a settled imported
    plugin (imported-plugin-revision-adjustment-fix, 2.1-2.5): the
    dead-end fix for the incompatible-platform warning's
    suggestedRevision. Resolves the requested revision to a `fetches`
    slot via adjustment_fetch_slot:

      - reuse: the revision's tree already synced (status 'succeeded')
        — map arch_revisions[arch] to it, re-queue the arch, and start
        the build immediately (2.2);
      - join: a concurrent adjustment is already fetching the revision
        — the arch joins its pending_archs and waits for that result;
      - fetch: StartBuild the fetch step for the revision's own
        rev-{slug}/ prefix (REVISION_SLUG attribution) and record the
        'fetching' entry with pending_archs = [arch].
        arch_revisions[arch] is NOT changed yet: it flips only when
        the fetch succeeds (_handle_adjustment_fetch_result), so a
        fetch failure leaves the prior mapping intact (2.4).

    Every path re-queues ONLY the adjusted architecture's artifact
    entry and REMOVEs components_triggered (a new build round, 3.6).
    The record's source_s3_prefix, default_fetch_slug, plugins_found,
    selected_plugins, and every other architecture's entry are never
    written (3.4, 3.5). Requires node-designer:manage (2.5); rejected
    with 409 for records that are not repository imports or whose
    import has not settled to 'imported'.
    """
    body, err = parse_body(event)
    if err:
        return err

    item = get_version_item(plugin_id, version)
    if not item:
        return not_found_response()

    architectures = [str(a) for a in item.get('requested_architectures') or []]
    architecture = body.get('architecture')
    if not isinstance(architecture, str) or architecture not in architectures:
        return error_response(
            400, 'INVALID_ARCHITECTURE',
            "architecture must be one of this version's requested "
            'Target_Architectures',
            {'requested_architectures': architectures})

    revision = body.get('revision')
    if not isinstance(revision, str) or not revision.strip():
        return error_response(400, 'INVALID_REVISION',
                              'revision must be a non-empty string')
    revision = revision.strip()

    err = authorize_record_access(user, event, item, manage=True,
                                  permission=Permission.NODE_DESIGNER_MANAGE)
    if err:
        return err

    provenance = item.get('provenance') or {}
    if (item.get('kind') != 'imported' or not provenance.get('repoUrl')
            or item.get('import_status') != IMPORT_STATUS_IMPORTED):
        return error_response(
            409, 'REVISION_ADJUSTMENT_NOT_AVAILABLE',
            'Per-platform revision adjustment is only available for '
            'repository imports whose import has settled',
            {'kind': item.get('kind'),
             'import_status': item.get('import_status')})

    fetches = item.get('fetches') or {}
    slug, action = adjustment_fetch_slot(item, revision)

    # One update writes the whole adjustment: the adjusted arch
    # re-queued and components_triggered REMOVEd (a new build round,
    # exactly like start_builds) plus the action-specific mutation.
    # Nothing else on the record is written (3.4, 3.5).
    names: Dict[str, str] = {'#a': architecture}
    values: Dict = {':q': {'buildStatus': BUILD_QUEUED}, ':t': now_ms()}
    sets = ['artifacts.#a = :q', 'updated_at = :t']
    fetch_build_id: Optional[str] = None

    if action == ADJUST_REUSE:
        # The revision's tree is already synced: map the arch to it now
        # (creating arch_revisions when the record is still flat).
        if item.get('arch_revisions') is not None:
            values[':slug'] = slug
            sets.append('arch_revisions.#a = :slug')
        else:
            values[':ar'] = {architecture: slug}
            sets.append('arch_revisions = :ar')
    elif action == ADJUST_JOIN:
        # A concurrent adjustment is fetching this revision: the arch
        # joins its pending_archs and settles with that fetch result.
        pending = [str(a) for a
                   in (fetches.get(slug) or {}).get('pending_archs') or []]
        if architecture not in pending:
            pending.append(architecture)
        names['#s'] = slug
        values[':pa'] = pending
        sets.append('fetches.#s.pending_archs = :pa')
    else:  # ADJUST_FETCH: fresh slug, or a failed entry reset in place
        base_prefix = source_s3_prefix(item['usecase_id'], plugin_id,
                                       version)
        entry = {
            'revision': revision,
            'source_prefix': f'{base_prefix}rev-{slug}/',
            'status': FETCH_STATUS_FETCHING,
            'pending_archs': [architecture],
        }
        try:
            fetch_build_id = start_fetch(
                provenance['repoUrl'],
                (None if revision == DEFAULT_REVISION else revision),
                entry['source_prefix'].rstrip('/'),
                usecase_id=item['usecase_id'], plugin_id=plugin_id,
                version=version, revision_slug_id=slug)
        except Exception as exc:
            logger.error(f'Adjustment fetch StartBuild failed: {exc}',
                         exc_info=True)
            log_audit_event(
                user_id=user['user_id'],
                action='adjust_plugin_revision',
                resource_type='plugin_record',
                resource_id=plugin_id,
                result='failure',
                details={'usecase_id': item['usecase_id'],
                         'version': version,
                         'architecture': architecture,
                         'revision': revision,
                         'reason': 'REPO_FETCH_FAILED'}
            )
            return error_response(
                502, 'REPO_FETCH_FAILED',
                'The adjusted revision fetch could not be started',
                {'revision': revision})
        entry['fetch_build_id'] = fetch_build_id
        if fetches:
            names['#s'] = slug
            values[':fe'] = entry
            sets.append('fetches.#s = :fe')
        else:
            values[':fm'] = {slug: entry}
            sets.append('fetches = :fm')

    plugin_table().update_item(
        Key={'plugin_id': plugin_id, 'version': version},
        UpdateExpression=('SET ' + ', '.join(sets)
                          + ' REMOVE components_triggered'),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )

    if action == ADJUST_REUSE:
        # The adjusted arch is queued with its mapping in place: start
        # its build now; only its entry is queued, so the start touches
        # nothing else. Auto-start failure never fails the adjustment.
        _start_queued_builds(plugin_id, version)

    log_audit_event(
        user_id=user['user_id'],
        action='adjust_plugin_revision',
        resource_type='plugin_record',
        resource_id=plugin_id,
        result='success',
        details={'usecase_id': item['usecase_id'], 'version': version,
                 'architecture': architecture, 'revision': revision,
                 'revision_slug': slug, 'mode': action,
                 **({'fetch_build_id': fetch_build_id}
                    if fetch_build_id else {})}
    )

    updated = get_version_item(plugin_id, version)
    # plugin_builds lazily imports this module for fetch results, so a
    # module-level import would be circular (mirrors _start_queued_builds).
    import plugin_builds
    return create_response(202, {
        'plugin': import_detail(updated),
        'builds': plugin_builds.builds_view(updated),
    })


def list_module_plugins(event: Dict, user: Dict, module: str) -> Dict:
    """
    GET /plugin-modules?module=<name>
    The individual plugins of one official GStreamer module, so the
    import view offers a plugin selection before the import.

    Returns {module, plugins: [{name, description?}], fetchedAt,
    cached}. The list is sourced from the GitLab API for the gstreamer
    monorepo (each subdirectory of subprojects/<module>/{gst,ext,sys}/
    is one plugin), per-plugin descriptions joined from the module's
    docs/gst_plugins_cache.json metadata (an enhancement — a
    description fetch/parse failure never fails the listing),
    and cached per module in the ModuleIndexCache table with the same
    24-hour TTL pattern as the module index. Fetch failure — or a
    module without any enumerable plugin — returns the existing
    MODULE_LISTING_UNAVAILABLE code so the UI falls back to importing
    the full set (selection is an enhancement, never a blocker).
    """
    module = (module or '').strip()
    if not _MODULE_NAME_RE.match(module):
        return error_response(400, 'INVALID_MODULE',
                              'module must be a module name from the '
                              'GStreamer module listing')

    now = now_ms()
    cached = read_cached_module_plugins(module)
    if cached and module_index_is_fresh(cached, now):
        return create_response(200, {
            'module': module,
            'plugins': cached.get('plugins', []),
            'fetchedAt': cached.get('fetchedAt'),
            'cached': True,
        })

    try:
        trees = fetch_module_plugin_trees(module)
        plugins = module_plugins_from_trees(trees)
        if not plugins:
            raise ModuleListingParseError(
                f'module {module} lists no individual plugins')
    except Exception as exc:
        logger.warning(f'Module plugin list unavailable for {module}: {exc}')
        return error_response(
            502, 'MODULE_LISTING_UNAVAILABLE',
            f"The plugin list for module '{module}' could not be "
            'retrieved; the import proceeds with the full plugin set',
            {'module': module, 'source': MODULE_PLUGINS_TREE_URL})

    # Join per-plugin descriptions from the module's metadata cache —
    # an enhancement only: on any failure the entries simply lack
    # descriptions. The joined entries are what gets cached.
    plugins = join_plugin_descriptions(
        plugins, fetch_module_plugin_descriptions(module))

    write_module_plugins_cache(module, plugins, now)
    return create_response(200, {
        'module': module,
        'plugins': plugins,
        'fetchedAt': now,
        'cached': False,
    })


def list_plugin_modules(event: Dict, user: Dict) -> Dict:
    """
    GET /plugin-modules
    Module_Listing fetch/parse/cache (Requirement 6).

    Returns {modules: [{name, description, repoUrl, classification}],
    fetchedAt, cached}. A cached index younger than 24 hours is reused
    (6.4); otherwise the listing is fetched and parsed server-side and
    the cache refreshed (6.1). Fetch or parse failure returns the
    distinct MODULE_LISTING_UNAVAILABLE code so the UI offers manual
    repository URL entry as the alternative import path (6.3). The
    entries' repoUrl is the module's published repository location,
    which the UI feeds into POST /plugins/import on selection (6.2).

    Read-only global data: available to every authenticated portal user
    (node-designer:read is granted to all roles, 13.3).
    """
    now = now_ms()

    cached = read_cached_module_index()
    if cached and module_index_is_fresh(cached, now):
        return create_response(200, {
            'modules': cached.get('modules', []),
            'fetchedAt': cached.get('fetchedAt'),
            'cached': True,
        })

    try:
        page = fetch_module_listing()
        modules = parse_module_listing(page)
    except Exception as exc:
        logger.warning(f'Module_Listing unavailable: {exc}')
        return error_response(
            502, 'MODULE_LISTING_UNAVAILABLE',
            'The official GStreamer module listing could not be retrieved '
            'or parsed; enter a repository URL manually to import a plugin',
            {'source': MODULE_LISTING_URL})

    write_module_index_cache(modules, now)
    return create_response(200, {
        'modules': modules,
        'fetchedAt': now,
        'cached': False,
    })


# ------------------------------------------------------------------ routing

def handler(event: Dict, context) -> Dict:
    """Main Lambda handler - routes to the appropriate operation"""
    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        user = get_user_from_event(event)
        resource = event.get('resource', '')

        if resource == '/plugins/import' and http_method == 'POST':
            return import_repository(event, user)
        if resource == '/plugin-modules' and http_method == 'GET':
            # Optional ?module=<name>: the individual plugins of one
            # module (no new API Gateway route needed).
            params = event.get('queryStringParameters') or {}
            module = params.get('module')
            if module is not None:
                return list_module_plugins(event, user, module)
            return list_plugin_modules(event, user)
        if (resource == '/plugins/{id}/versions/{v}/select-plugins'
                and http_method == 'POST'):
            path_params = event.get('pathParameters') or {}
            plugin_id = path_params.get('id')
            try:
                version = int(path_params.get('v'))
            except (TypeError, ValueError):
                return error_response(400, 'INVALID_VERSION',
                                      'version must be an integer')
            if plugin_id:
                return select_plugins(event, user, plugin_id, version)
        if (resource == '/plugins/{id}/versions/{v}/adjust-revision'
                and http_method == 'POST'):
            path_params = event.get('pathParameters') or {}
            plugin_id = path_params.get('id')
            try:
                version = int(path_params.get('v'))
            except (TypeError, ValueError):
                return error_response(400, 'INVALID_VERSION',
                                      'version must be an integer')
            if plugin_id:
                return adjust_revision(event, user, plugin_id, version)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
