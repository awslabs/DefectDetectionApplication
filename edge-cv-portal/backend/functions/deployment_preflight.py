"""Pre-submit deployment closure validation.

Spec: .kiro/specs/deployment-preflight-validation/
Live evidence: .kiro/specs/deployment-preflight-validation/evidence.md

ONE capability, asked THREE questions of ONE resolved closure (tasks 4.1-4.5):

1. **Platform satisfiability** (bugfix.md 2.2, 2.3, 2.7, 2.8 as corrected by
   evidence.md §5.1) — does every component version in the closure publish a
   manifest the target device's platform satisfies?
2. **Dependency resolvability** (2.4, 2.5) — does every depended-on name have
   at least one published version satisfying its ``VersionRequirement``, in
   EITHER the account namespace or the AWS-managed ``aws`` namespace?
3. **De-selected but still required** (2.11-2.15) — is anything the operator
   removed still required by something they kept?

Every finding is classified as exactly one of ``blocking-invalid`` (2.3, 2.5 —
no acknowledgement bypasses it), ``acknowledgement-required`` (2.13, 2.14 —
refused on first submit, proceeds on a re-submit carrying a matching SPECIFIC
acknowledgement) or ``unverified`` (2.9 — reported, blocks nothing), and they
are reported in ONE pass (2.6, 2.16).

Design decisions worth knowing before changing anything here
------------------------------------------------------------
* **One closure, three validators — not three walks.** ``resolve_closure``
  fetches each ``(name, version)`` recipe AT MOST ONCE and each name's
  published-version list AT MOST ONCE for the whole validation; the three
  validators are pure functions over the resolved object. The recipe read
  generalizes the one production read that exists today
  (``deployments.resolve_public_component_version``,
  ``deployments.py:437-450``).
* **Platforms come from the recipe, not ``DescribeComponent``.**
  ``describe-component`` mirrors the recipe exactly (187/187, evidence.md
  §1.1) but carries no ``ComponentDependencies``, so using it would double
  the calls per node for information the closure walk already holds.
* **Fail-OPEN is a hard contract (2.9).** Every injected callable is wrapped;
  ANY exception (throttling, AccessDenied, ResourceNotFound, a malformed or
  non-JSON recipe body, a broken paginator) becomes an ``unverified`` finding
  and NEVER propagates out of the resolver or the validators. This is
  deliberately the OPPOSITE of ``deployments.evaluate_plugin_arch_gate``,
  which fails CLOSED on a device with no recorded ``Target_Architecture``;
  bugfix.md 2.9 states the asymmetry is intentional and 3.4 pins the plugin
  gate, so both coexist unchanged.
* **Dual namespace or every deployment is blocked.**
  ``list-component-versions`` against the WRONG namespace returns an EMPTY
  LIST rather than raising (evidence.md §1.1, §5.6), so exception handling
  cannot substitute for querying both. ``aws.greengrass.Nucleus`` / ``Cli`` /
  ``ShadowManager`` / ``LogManager`` / ``SecureTunneling`` publish ONLY under
  ``aws`` and are auto-included on every portal deployment.
* **The wildcard matcher rules are load-bearing, not a convenience.** An
  ABSENT attribute key is a wildcard, a literal ``"*"`` value is a wildcard,
  and an empty/null ``Platform`` matches everything (evidence.md §1.1: 107 of
  the account's 148 aarch64 manifests are variant-less, Nucleus publishes
  ``{"os":"linux"}`` with no architecture, ShadowManager and LogManager
  publish ``{"os":"*"}``). A stricter matcher reports Nucleus and
  ShadowManager incompatible with every device and refuses EVERY submission.
* **The device's variant comes only from the portal's own record.**
  ``get_core_device`` returns ``platform``/``architecture``/``runtime`` and NO
  ``variant``, byte-identically for JP5, JP6 and JP7 devices (evidence.md
  §1.3). ``DEVICES_TABLE.target_architecture`` maps to the manifest
  ``variant`` by IDENTITY (``arm64_jp7`` -> ``variant: arm64_jp7``); absent,
  the variant is UNKNOWN and a variant-bearing manifest is UNVERIFIED, never
  incompatible.
* **An unpinned ``>=0.0.0`` edge is not a resolvability fault.** A
  requirement every version satisfies is also satisfied by whatever the
  device already holds — that is exactly how Counterexample C's removed
  component stayed resolved (``ComponentManager: Found running component
  which meets the requirement and use it``) — and the portal cannot prove
  otherwise pre-submit, because ``ListInstalledComponents`` is
  device-reported, stale, and ROOT-filtered by default (evidence.md §1.2,
  §5.8). ``workflow_packaging.model_component_dependencies`` emits exactly
  this edge by design, one per resolved model component, and its documented
  job is the ordering/health edge rather than version pinning (3.12). So an
  unpinned edge whose name has no published version is UNVERIFIED (2.9), not
  blocking; when the name was DE-SELECTED it is the 2.11-2.15 finding
  instead, which is the question that edge really raises.

Nothing here reads AWS directly: the resolver is pure over injected callables
so the property suites need no account. ``greengrass_fetchers`` builds the
production callables (memoized, dual-namespace, exception-wrapped).
"""
import copy
import json
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response contract (pinned by
# edge-cv-portal/backend/tests/test_deployment_preflight_exploration.py)
# ---------------------------------------------------------------------------

#: Any blocking-invalid finding present.
CODE_VALIDATION_FAILED = 'PREFLIGHT_VALIDATION_FAILED'
#: The ONLY findings are acknowledgement-required, and the acknowledgement
#: does not match the computed finding set exactly.
CODE_ACKNOWLEDGEMENT_REQUIRED = 'PREFLIGHT_ACKNOWLEDGEMENT_REQUIRED'
#: Additive request field carrying the SPECIFIC acknowledgement (2.14); the
#: `confirmed_warnings` precedent on the workflow submit path.
ACKNOWLEDGEMENT_FIELD = 'acknowledged_retained_components'

KIND_PLATFORM = 'platform-mismatch'
KIND_DEPENDENCY = 'dependency-unresolvable'
KIND_DESELECTED = 'deselected-still-required'

CLASS_BLOCKING = 'blocking-invalid'
CLASS_ACKNOWLEDGEMENT = 'acknowledgement-required'
CLASS_UNVERIFIED = 'unverified'

VALIDATION_FAILED_MESSAGE = (
    'One or more selected components, or components in their dependency '
    'closure, cannot be deployed to the target devices; the deployment was '
    'not submitted')
ACKNOWLEDGEMENT_REQUIRED_MESSAGE = (
    'One or more de-selected components will remain installed on the target '
    'devices as resolved dependencies of components that are still selected; '
    'the deployment was not submitted. Re-submit acknowledging the named '
    'components to proceed, or de-select the components that require them')

# ---------------------------------------------------------------------------
# Walk bounds. A recipe graph can be cyclic or pathological; hitting either
# bound yields UNVERIFIED findings (2.9), never a fault and never a hang.
# ---------------------------------------------------------------------------

DEFAULT_MAX_DEPTH = 12
DEFAULT_MAX_NODES = 400

REASON_DEPTH_BOUND = 'closure depth bound reached'
REASON_NODE_BOUND = 'closure size bound reached'
REASON_NO_MANIFESTS = 'the component version publishes no platform manifests'
REASON_NO_VERSION = 'no component version is pinned for this entry'

#: Platform attribute values that constrain nothing (evidence.md §1.1).
WILDCARD_ATTRIBUTE_VALUES = frozenset({'*', '', None})

#: The only non-identity entry in the device-platform map: the portal records
#: `x86_64_nvidia` as a Target_Architecture, which manifests express as the
#: `runtime` attribute (evidence.md §1.3).
NVIDIA_TARGET_ARCHITECTURE = 'x86_64_nvidia'
NVIDIA_RUNTIME = 'nvidia'

#: Namespace of the AWS-managed public components.
AWS_NAMESPACE = 'aws'
AWS_MANAGED_PREFIX = 'aws.greengrass.'


# ---------------------------------------------------------------------------
# Version arithmetic
#
# Mirrors deployments._version_key / deployments._nucleus_satisfies EXACTLY
# (space-separated comparators, AND-ed; an empty/unparseable requirement is
# satisfied) so a dependency this module judges resolvable is one the
# production Nucleus resolution would also accept. Duplicated rather than
# imported to keep this module free of any dependency on deployments.py —
# deployments.py imports THIS module.
# ---------------------------------------------------------------------------

_COMPARATOR = re.compile(r'^(>=|<=|==|=|>|<)?\s*(.+)$')


def version_key(version):
    """Comparable tuple for a component version; non-numeric parts sort last."""
    parts = []
    for part in str(version).split('.'):
        try:
            parts.append((0, int(part)))
        except ValueError:
            parts.append((1, part))
    return tuple(parts)


def requirement_satisfied(version, requirement):
    """True when ``version`` satisfies a Greengrass ``VersionRequirement``."""
    if not requirement:
        return True
    candidate = version_key(version)
    for token in str(requirement).split():
        token = token.strip()
        if not token:
            continue
        match = _COMPARATOR.match(token)
        if not match:
            continue
        operator = match.group(1) or '=='
        bound = version_key(match.group(2))
        if operator in ('=', '==') and candidate != bound:
            return False
        if operator == '>=' and not candidate >= bound:
            return False
        if operator == '<=' and not candidate <= bound:
            return False
        if operator == '>' and not candidate > bound:
            return False
        if operator == '<' and not candidate < bound:
            return False
    return True


def is_unpinned_requirement(requirement):
    """True for a requirement EVERY version satisfies.

    Such an edge cannot be judged unresolvable from the cloud alone: any
    version the device already holds satisfies it (Counterexample C), and it
    is the shape ``workflow_packaging.model_component_dependencies`` emits by
    design (3.12). See the module docstring.
    """
    text = str(requirement or '').strip()
    if not text:
        return True
    return text.replace(' ', '') in ('*', '>=0.0.0', '>=0')


def newest(versions):
    return sorted(versions, key=version_key, reverse=True)


# ---------------------------------------------------------------------------
# Platform satisfaction (2.8 as corrected by evidence.md §5.1)
# ---------------------------------------------------------------------------

def manifest_satisfied(manifest_platform, device_platform):
    """Does ``device_platform`` satisfy one manifest ``Platform`` block?

    Returns True (satisfied), False (contradicted) or None (undecidable —
    the manifest constrains an attribute the device does not report).

    A manifest is satisfied when every attribute it CONSTRAINS matches. An
    absent attribute key, a literal ``"*"`` value and an empty/null
    ``Platform`` block all constrain nothing.
    """
    if not manifest_platform:
        # `Platform: null` / `{}` — matches every device (testmodel,
        # alienmodel; describe-component reports `{"attributes": {}}`).
        return True
    undecidable = False
    for key, value in manifest_platform.items():
        if value in WILDCARD_ATTRIBUTE_VALUES:
            continue                      # literal wildcard
        reported = (device_platform or {}).get(key)
        if reported in (None, ''):
            undecidable = True            # the device does not report it
            continue
        if str(reported) != str(value):
            return False
    return None if undecidable else True


def platforms_satisfied(manifest_platforms, device_platform):
    """Verdict over ALL of a component version's manifests: True when any
    manifest is satisfied, False when every manifest is contradicted, None
    when none is satisfied but at least one is undecidable."""
    if not manifest_platforms:
        return None
    verdicts = [manifest_satisfied(platform, device_platform)
                for platform in manifest_platforms]
    if True in verdicts:
        return True
    if None in verdicts:
        return None
    return False


def resolve_device_platforms(thing_names, read_core_device,
                            device_architectures=None):
    """The platform attributes of each target device, KNOWN attributes only.

    ``os`` / ``architecture`` come from ``get_core_device``; ``variant`` comes
    ONLY from the portal's own ``DEVICES_TABLE.target_architecture`` by
    identity map; ``runtime`` is derived from the single non-identity entry in
    that map (evidence.md §1.3). An attribute that cannot be read is simply
    ABSENT from the result, which makes any manifest constraining it
    UNVERIFIED rather than incompatible (2.9, 3.8).
    """
    device_architectures = device_architectures or {}
    platforms = {}
    for thing_name in thing_names or []:
        platform = {}
        try:
            reported = read_core_device(thing_name) or {}
        except Exception as error:                 # noqa: BLE001 — fail open
            logger.info('preflight: could not read core device %s: %s',
                        thing_name, error)
            reported = {}
        if reported.get('platform'):
            platform['os'] = str(reported['platform'])
        if reported.get('architecture'):
            platform['architecture'] = str(reported['architecture'])
        recorded = device_architectures.get(thing_name)
        if recorded:
            platform['variant'] = str(recorded)
            if str(recorded) == NVIDIA_TARGET_ARCHITECTURE:
                platform['runtime'] = NVIDIA_RUNTIME
        platforms[thing_name] = platform
    return platforms


# ---------------------------------------------------------------------------
# The resolved closure (task 4.1)
# ---------------------------------------------------------------------------

class ClosureNode:
    """One ``(component_name, version)`` in the closure."""

    __slots__ = ('component_name', 'component_version', 'platforms',
                 'dependencies', 'is_root', 'depth')

    def __init__(self, component_name, component_version, platforms,
                 dependencies, is_root, depth):
        self.component_name = component_name
        self.component_version = component_version
        #: the manifest ``Platform`` blocks, verbatim (``None`` entries kept)
        self.platforms = platforms
        #: {name: {'VersionRequirement': str, 'DependencyType': str}}
        self.dependencies = dependencies
        self.is_root = is_root
        self.depth = depth

    @property
    def key(self):
        return (self.component_name, self.component_version)


class ClosureEdge:
    """One ``ComponentDependencies`` entry, with who declared it."""

    __slots__ = ('component_name', 'version_requirement', 'dependency_type',
                 'required_by_name', 'required_by_version',
                 'required_by_is_root', 'resolved_version')

    def __init__(self, component_name, version_requirement, dependency_type,
                 required_by_name, required_by_version, required_by_is_root):
        self.component_name = component_name
        self.version_requirement = version_requirement
        self.dependency_type = dependency_type
        self.required_by_name = required_by_name
        self.required_by_version = required_by_version
        self.required_by_is_root = required_by_is_root
        self.resolved_version = None

    def requirer(self):
        return {'component_name': self.required_by_name,
                'component_version': self.required_by_version,
                'version_requirement': self.version_requirement,
                'dependency_type': self.dependency_type}


class ResolvedClosure:
    """The single resolved closure all three validators read.

    ``nodes``       {(name, version): ClosureNode}
    ``edges``       [ClosureEdge] — every dependency entry reached
    ``published``   {name: [versions]} per-name published versions, the union
                    of both namespaces; ``None`` when the list is unreadable
    ``unresolved``  [(name, version, reason)] — nodes that could not be read
    ``roots``       {name: version or None} the submitted set
    ``recipe_reads`` / ``version_reads`` — call counters (task 5.4's property)
    """

    def __init__(self, roots):
        self.roots = dict(roots or {})
        self.nodes = {}
        self.edges = []
        self.published = {}
        self.unresolved = []
        self.recipe_reads = 0
        self.version_reads = 0

    # -- convenience -----------------------------------------------------
    def published_versions(self, component_name):
        return self.published.get(component_name)

    def edges_requiring(self, component_name):
        return [edge for edge in self.edges
                if edge.component_name == component_name]


def resolve_closure(roots, fetch_recipe, list_versions, describe=None,
                    max_depth=DEFAULT_MAX_DEPTH, max_nodes=DEFAULT_MAX_NODES):
    """Resolve the transitive ``ComponentDependencies`` closure of ``roots``.

    ``roots``         {component_name: component_version or None} — the FINAL
                      submitted component set (auto-includes included).
    ``fetch_recipe``  ``(name, version) -> recipe`` (dict, or a JSON str/bytes
                      body as ``GetComponent`` returns). MUST be memoized by
                      the caller; may raise.
    ``list_versions`` ``(name) -> [version]`` across BOTH namespaces. MUST be
                      memoized by the caller; may raise.
    ``describe``      accepted and ignored: ``DescribeComponent`` carries no
                      ``ComponentDependencies``, so the walk reads the recipe
                      it needs anyway (evidence.md §1.1, §5.2).

    Cycle-safe (a ``(name, version)`` is visited once) and depth-bounded.
    NEVER raises: any failure becomes an ``unresolved`` entry, which the
    validators report as UNVERIFIED.
    """
    closure = ResolvedClosure(roots)
    if describe is not None:
        logger.debug('preflight: describe_component is not used by the '
                     'closure walk; platforms come from the recipe')

    def published_for(component_name):
        if component_name in closure.published:
            return closure.published[component_name]
        try:
            closure.version_reads += 1
            versions = [str(version) for version in
                        (list_versions(component_name) or [])]
        except Exception as error:                 # noqa: BLE001 — fail open
            logger.info('preflight: could not list versions of %s: %s',
                        component_name, error)
            versions = None
        closure.published[component_name] = versions
        return versions

    def recipe_for(component_name, component_version):
        closure.recipe_reads += 1
        recipe = fetch_recipe(component_name, component_version)
        if isinstance(recipe, (bytes, bytearray)):
            recipe = recipe.decode('utf-8')
        if isinstance(recipe, str):
            recipe = json.loads(recipe)
        if not isinstance(recipe, dict):
            raise ValueError(f'recipe is {type(recipe).__name__}, not an object')
        return recipe

    # (name, version, is_root, depth); a queue rather than recursion so a
    # pathological graph cannot blow the stack.
    pending = []
    for name in sorted(closure.roots):
        pending.append((name, closure.roots.get(name), True, 0))
    visited = set()

    while pending:
        component_name, component_version, is_root, depth = pending.pop(0)
        if not component_version:
            # An unpinned entry (the auto-included Nucleus fallback): there is
            # no version to read a recipe for, so nothing about it can be
            # verified. Never a fault (2.9).
            closure.unresolved.append(
                (component_name, None, REASON_NO_VERSION))
            continue
        key = (component_name, str(component_version))
        if key in visited:
            continue
        visited.add(key)
        if len(closure.nodes) >= max_nodes:
            closure.unresolved.append((component_name, component_version,
                                       REASON_NODE_BOUND))
            continue
        if depth > max_depth:
            closure.unresolved.append((component_name, component_version,
                                       REASON_DEPTH_BOUND))
            continue

        try:
            recipe = recipe_for(component_name, component_version)
        except Exception as error:                 # noqa: BLE001 — fail open
            logger.info('preflight: could not read recipe of %s %s: %s',
                        component_name, component_version, error)
            closure.unresolved.append(
                (component_name, component_version, str(error) or
                 'the component recipe could not be read'))
            continue

        platforms = []
        manifests = recipe.get('Manifests')
        if isinstance(manifests, list):
            for manifest in manifests:
                if isinstance(manifest, dict):
                    platforms.append(manifest.get('Platform'))
        dependencies = recipe.get('ComponentDependencies') or {}
        if not isinstance(dependencies, dict):
            dependencies = {}

        node = ClosureNode(component_name, str(component_version), platforms,
                           dependencies, is_root, depth)
        closure.nodes[node.key] = node

        for dependency_name in sorted(dependencies):
            entry = dependencies.get(dependency_name) or {}
            if not isinstance(entry, dict):
                entry = {}
            edge = ClosureEdge(
                component_name=str(dependency_name),
                version_requirement=str(entry.get('VersionRequirement') or ''),
                dependency_type=str(entry.get('DependencyType') or ''),
                required_by_name=node.component_name,
                required_by_version=node.component_version,
                required_by_is_root=node.is_root)
            closure.edges.append(edge)

            # Which version of the dependency does the deployment get? The
            # submitted set pins it when it carries the name AND that pin
            # satisfies the requirement (a pin that does NOT satisfy it is
            # exactly the fault Greengrass rejects, so it must not be taken
            # as resolution); otherwise the newest published version
            # satisfying the requirement, which is what Greengrass
            # negotiates.
            pinned = closure.roots.get(edge.component_name)
            if pinned and requirement_satisfied(pinned,
                                                edge.version_requirement):
                edge.resolved_version = str(pinned)
            else:
                # published_for is memoized, so the resolvability validator
                # can read closure.published for every UNRESOLVED edge
                # without the fetchers, and no name is listed twice.
                candidates = published_for(edge.component_name) or []
                satisfying = [version for version in candidates
                              if requirement_satisfied(
                                  version, edge.version_requirement)]
                if satisfying:
                    edge.resolved_version = newest(satisfying)[0]
            if edge.resolved_version:
                pending.append((edge.component_name, edge.resolved_version,
                                False, depth + 1))

    return closure


# ---------------------------------------------------------------------------
# Validator A — platform satisfiability (task 4.2, requirements 2.2/2.3/2.7/2.8)
# ---------------------------------------------------------------------------

def _platform_remediation(component_name, component_version, claimed):
    variants = sorted({str((platform or {}).get('variant'))
                       for platform in claimed
                       if (platform or {}).get('variant')})
    if variants:
        return (f'{component_name} {component_version} publishes manifests '
                f'for {", ".join(variants)} only. Re-package or re-register '
                f'it for the target device(s) JetPack/platform, or de-select '
                f'it and deploy a build that targets them.')
    return (f'{component_name} {component_version} publishes no manifest the '
            f'target device(s) satisfy. Re-package or re-register it for the '
            f'target platform, or de-select it.')


def validate_platforms(closure, device_platforms):
    """Findings for every component version in the ALREADY-resolved closure
    whose published manifests no target device satisfies (2.3), plus
    UNVERIFIED findings where the judgement could not be made (2.9, 3.8).

    The component's CLAIMED platforms and each device's REPORTED platform are
    carried as distinct facts (2.7): the Greengrass wording that presents the
    device's platform as the component's claim is never reproduced.
    """
    findings = []
    devices = sorted(device_platforms or {})

    for key in sorted(closure.nodes):
        node = closure.nodes[key]
        claimed = [copy.deepcopy(platform) for platform in node.platforms]
        if not claimed:
            findings.append({
                'kind': KIND_PLATFORM,
                'finding_class': CLASS_UNVERIFIED,
                'component_name': node.component_name,
                'component_version': node.component_version,
                'claimed_platforms': [],
                'reason': REASON_NO_MANIFESTS,
                'remediation': (
                    f'{node.component_name} {node.component_version} could '
                    f'not be checked against the target platform; Greengrass '
                    f'remains the authoritative check at submit time.'),
            })
            continue
        if not devices:
            continue

        incompatible = []
        undecidable = []
        for thing_name in devices:
            verdict = platforms_satisfied(node.platforms,
                                          device_platforms[thing_name])
            entry = {'thing_name': thing_name,
                     'platform': dict(device_platforms[thing_name] or {})}
            if verdict is False:
                incompatible.append(entry)
            elif verdict is None:
                undecidable.append(entry)

        if incompatible:
            findings.append({
                'kind': KIND_PLATFORM,
                'finding_class': CLASS_BLOCKING,
                'component_name': node.component_name,
                'component_version': node.component_version,
                'claimed_platforms': claimed,
                'devices': incompatible,
                'unverified_devices': undecidable,
                'required_by': [edge.requirer() for edge in closure.edges
                                if edge.component_name == node.component_name
                                and edge.resolved_version ==
                                node.component_version],
                'remediation': _platform_remediation(
                    node.component_name, node.component_version, claimed),
            })
        elif undecidable:
            findings.append({
                'kind': KIND_PLATFORM,
                'finding_class': CLASS_UNVERIFIED,
                'component_name': node.component_name,
                'component_version': node.component_version,
                'claimed_platforms': claimed,
                'devices': undecidable,
                'reason': (
                    'the target device(s) do not report every platform '
                    'attribute this component version constrains'),
                'remediation': (
                    f'{node.component_name} {node.component_version} could '
                    f'not be judged against '
                    f'{", ".join(entry["thing_name"] for entry in undecidable)}'
                    f'; record the device architecture in the portal to have '
                    f'it checked before submit.'),
            })
    return findings


# ---------------------------------------------------------------------------
# Validator B — dependency resolvability (task 4.3, requirements 2.4/2.5)
# ---------------------------------------------------------------------------

def _dependency_remediation(component_name, requirement, has_versions,
                            requirers):
    names = ', '.join(sorted({
        f'{entry["component_name"]} {entry["component_version"]}'
        for entry in requirers}))
    if has_versions:
        return (f'No published version of {component_name} satisfies '
                f'{requirement!r}, required by {names}. Deploy a version of '
                f'{names} whose dependency matches a published '
                f'{component_name} version, or publish one that satisfies it.')
    return (f'{component_name} has no published version in this account, in '
            f'either the account or the AWS-managed namespace, yet {names} '
            f'requires it at {requirement!r}. {names} cannot deploy until it '
            f'is re-registered or repackaged against a component that exists '
            f'for the target platform.')


def validate_resolvability(closure):
    """Findings for every dependency edge in the ALREADY-resolved closure
    whose depended-on name has no published version satisfying its
    ``VersionRequirement`` (2.5).

    Grouped by ``(name, VersionRequirement)`` so one response carries one
    finding per distinct unresolvable requirement, listing every component
    that requires it. Distinguishes ZERO published versions from only
    NON-SATISFYING ones (2.5); an unreadable version list is UNVERIFIED
    (2.9); an unpinned ``>=0.0.0`` edge with no published version is
    UNVERIFIED rather than blocking (see the module docstring).
    """
    findings = []
    grouped = {}
    order = []
    for edge in closure.edges:
        if edge.resolved_version:
            continue
        group = (edge.component_name, edge.version_requirement)
        if group not in grouped:
            grouped[group] = []
            order.append(group)
        grouped[group].append(edge)

    for component_name, requirement in order:
        edges = grouped[(component_name, requirement)]
        requirers = []
        seen = set()
        for edge in edges:
            requirer = edge.requirer()
            token = (requirer['component_name'], requirer['component_version'])
            if token in seen:
                continue
            seen.add(token)
            requirers.append(requirer)

        published = closure.published_versions(component_name)
        if published is None:
            findings.append({
                'kind': KIND_DEPENDENCY,
                'finding_class': CLASS_UNVERIFIED,
                'component_name': component_name,
                'version_requirement': requirement,
                'required_by': requirers,
                'reason': ('the published versions of this component could '
                           'not be read'),
                'remediation': (
                    f'{component_name} could not be checked; Greengrass '
                    f'remains the authoritative check at submit time.'),
            })
            continue

        if not published and is_unpinned_requirement(requirement):
            # Satisfied by ANY version, including one the device already
            # holds — unprovable pre-submit, so never blocking (2.9).
            findings.append({
                'kind': KIND_DEPENDENCY,
                'finding_class': CLASS_UNVERIFIED,
                'component_name': component_name,
                'version_requirement': requirement,
                'required_by': requirers,
                'has_published_versions': False,
                'published_versions': [],
                'reason': (
                    'the requirement is satisfied by every version, so a '
                    'version already present on the device satisfies it; the '
                    'portal cannot verify that from the cloud'),
                'remediation': (
                    f'{component_name} has no published version in this '
                    f'account. Greengrass will resolve it from the device if '
                    f'a version is already installed there, and reject the '
                    f'deployment otherwise.'),
            })
            continue

        findings.append({
            'kind': KIND_DEPENDENCY,
            'finding_class': CLASS_BLOCKING,
            'component_name': component_name,
            'version_requirement': requirement,
            'required_by': requirers,
            'has_published_versions': bool(published),
            'published_versions': newest(published),
            'remediation': _dependency_remediation(
                component_name, requirement, bool(published), requirers),
        })
    return findings


# ---------------------------------------------------------------------------
# Validator C — de-selected but still required (task 4.4, 2.11-2.15)
# ---------------------------------------------------------------------------

def _deselected_effective_outcome(component_name, requirers):
    names = ', '.join(sorted({
        f'{entry["component_name"]} {entry["component_version"]}'
        for entry in requirers}))
    return (f'{component_name} will REMAIN installed and running on the '
            f'target device(s) as a resolved dependency of {names}, and will '
            f'NOT be removed from the device by this deployment.')


def validate_deselected_still_required(closure, previous_components,
                                       submitted_components,
                                       excluded_names=()):
    """Findings for every component the operator de-selected that a component
    they KEPT still requires (2.12, 2.13).

    ``previous_components``  {name: version or None} from the target's latest
                             deployment document.
    ``submitted_components`` {name: version or None} the FINAL submitted set.
    ``excluded_names``       portal auto-include names, excluded from the
                             diff so an auto-include that stopped applying is
                             never mistaken for an operator de-selection.
    """
    findings = []
    excluded = set(excluded_names or ())
    submitted = set(submitted_components or {})
    for component_name in sorted(previous_components or {}):
        if component_name in submitted or component_name in excluded:
            continue
        requirers = []
        seen = set()
        for edge in closure.edges_requiring(component_name):
            if edge.required_by_name not in submitted:
                # Only a component that REMAINS selected keeps it installed.
                continue
            requirer = edge.requirer()
            token = (requirer['component_name'], requirer['component_version'])
            if token in seen:
                continue
            seen.add(token)
            requirers.append(requirer)
        if not requirers:
            # 3.11: a removal with no remaining dependant is submitted
            # exactly as today — no finding, no acknowledgement step.
            continue
        findings.append({
            'kind': KIND_DESELECTED,
            'finding_class': CLASS_ACKNOWLEDGEMENT,
            'component_name': component_name,
            'component_version': (previous_components or {}).get(
                component_name),
            'required_by': requirers,
            'remains_installed': True,
            'removed_by_this_deployment': False,
            'effective_outcome': _deselected_effective_outcome(
                component_name, requirers),
            'remediation': (
                f'De-select the component(s) that require '
                f'{component_name} in the same edit and submit once, or '
                f're-submit with {ACKNOWLEDGEMENT_FIELD} naming exactly '
                f'{component_name} to accept that it stays installed.'),
        })
    return findings


# ---------------------------------------------------------------------------
# Single-pass classifier (task 4.5, requirements 2.6/2.7/2.9/2.10/2.16)
# ---------------------------------------------------------------------------

class PreflightOutcome:
    """The result of ONE validation pass.

    ``findings``  every finding, each classified exactly once
    ``refusal``   ``None`` when the submission proceeds, otherwise
                  ``{'code', 'message', 'details': {'findings': [...]}}``
                  ready for ``deployments._workflow_error(409, ...)``
    """

    __slots__ = ('findings', 'refusal', 'closure')

    def __init__(self, findings, refusal, closure=None):
        self.findings = findings
        self.refusal = refusal
        self.closure = closure

    @property
    def blocked(self):
        return self.refusal is not None

    def of_class(self, finding_class):
        return [finding for finding in self.findings
                if finding.get('finding_class') == finding_class]

    def summary(self):
        return {finding_class: len(self.of_class(finding_class))
                for finding_class in (CLASS_BLOCKING, CLASS_ACKNOWLEDGEMENT,
                                      CLASS_UNVERIFIED)}


_CLASS_ORDER = {CLASS_BLOCKING: 0, CLASS_ACKNOWLEDGEMENT: 1,
                CLASS_UNVERIFIED: 2}
_KIND_ORDER = {KIND_PLATFORM: 0, KIND_DEPENDENCY: 1, KIND_DESELECTED: 2}


def classify_findings(platform_findings, resolvability_findings,
                      deselected_findings, acknowledged=None):
    """Merge the three validators' output into ONE outcome (2.6, 2.16).

    * any blocking-invalid finding refuses, whatever the acknowledgement
      names (2.16) — an acknowledgement NEVER clears a blocking finding;
    * acknowledgement-required findings refuse UNLESS the acknowledgement
      matches the computed set EXACTLY (2.14): a superset, a subset, a stale
      or a blanket acknowledgement does not match, and a component-set change
      that alters the computed finding re-reports and refuses again;
    * unverified findings alone NEVER change the outcome (2.9, 2.10);
    * no finding at all ⇒ submit unchanged (2.10).
    """
    findings = list(platform_findings) + list(resolvability_findings) + \
        list(deselected_findings)
    findings.sort(key=lambda finding: (
        _CLASS_ORDER.get(finding.get('finding_class'), 9),
        _KIND_ORDER.get(finding.get('kind'), 9),
        str(finding.get('component_name')),
        str(finding.get('component_version') or ''),
        str(finding.get('version_requirement') or '')))

    acknowledged_set = {str(name) for name in (acknowledged or [])}
    computed = {str(finding['component_name'])
                for finding in findings
                if finding.get('finding_class') == CLASS_ACKNOWLEDGEMENT}
    # EXACT set equality, computed from the finding this submission produced.
    acknowledgement_matches = bool(computed) and acknowledged_set == computed
    for finding in findings:
        if finding.get('finding_class') == CLASS_ACKNOWLEDGEMENT:
            finding['acknowledged'] = acknowledgement_matches

    blocking = [finding for finding in findings
                if finding.get('finding_class') == CLASS_BLOCKING]
    if blocking:
        return PreflightOutcome(findings, {
            'code': CODE_VALIDATION_FAILED,
            'message': VALIDATION_FAILED_MESSAGE,
            'details': {'findings': findings},
        })
    if computed and not acknowledgement_matches:
        return PreflightOutcome(findings, {
            'code': CODE_ACKNOWLEDGEMENT_REQUIRED,
            'message': ACKNOWLEDGEMENT_REQUIRED_MESSAGE,
            'details': {'findings': findings,
                        'acknowledgement_field': ACKNOWLEDGEMENT_FIELD,
                        'acknowledgement_required_for': sorted(computed)},
        })
    return PreflightOutcome(findings, None)


# ---------------------------------------------------------------------------
# One entry point for both submit paths (task 4.6)
# ---------------------------------------------------------------------------

def evaluate(components_map, device_platforms, fetch_recipe, list_versions,
             previous_components=None, acknowledged=None, excluded_names=(),
             max_depth=DEFAULT_MAX_DEPTH, max_nodes=DEFAULT_MAX_NODES):
    """Resolve the closure ONCE and ask all three questions of it.

    ``components_map`` is the FINAL submitted map. It is deep-copied before
    anything reads it, so the validation cannot mutate what is about to be
    submitted (3.14).

    NEVER raises: any unexpected failure yields an empty outcome (fail open).
    """
    submitted = {}
    for name, entry in copy.deepcopy(dict(components_map or {})).items():
        submitted[str(name)] = (entry or {}).get('componentVersion')
    previous = {str(name): (entry or {}).get('componentVersion')
                for name, entry in
                copy.deepcopy(dict(previous_components or {})).items()}

    try:
        closure = resolve_closure(submitted, fetch_recipe, list_versions,
                                 max_depth=max_depth, max_nodes=max_nodes)
        platform_findings = validate_platforms(closure, device_platforms)
        platform_findings.extend(_unresolved_findings(closure))
        resolvability_findings = validate_resolvability(closure)
        deselected_findings = validate_deselected_still_required(
            closure, previous, submitted, excluded_names)
        outcome = classify_findings(platform_findings, resolvability_findings,
                                    deselected_findings, acknowledged)
        outcome.closure = closure
        return outcome
    except Exception as error:                     # noqa: BLE001 — fail open
        # Belt and braces: the resolver and validators already swallow every
        # account failure, so reaching here means a defect in THIS module.
        # A defect here must still never take deployment submission down.
        logger.warning('preflight: validation could not be completed (%s); '
                       'submitting unvalidated', error, exc_info=True)
        return PreflightOutcome([], None)


def _unresolved_findings(closure):
    """UNVERIFIED findings for the closure's unresolved nodes (2.9)."""
    findings = []
    for component_name, component_version, reason in closure.unresolved:
        findings.append({
            'kind': KIND_PLATFORM,
            'finding_class': CLASS_UNVERIFIED,
            'component_name': component_name,
            'component_version': component_version,
            'claimed_platforms': [],
            'reason': reason,
            'remediation': (
                f'{component_name} could not be checked before submit; '
                f'Greengrass remains the authoritative check.'),
        })
    return findings


# ---------------------------------------------------------------------------
# Production fetchers: dual namespace, memoized, exception-wrapped
# ---------------------------------------------------------------------------

def component_arn(region, namespace, component_name, component_version=None):
    arn = (f'arn:aws:greengrass:{region}:{namespace}:components:'
           f'{component_name}')
    if component_version:
        arn = f'{arn}:versions:{component_version}'
    return arn


def namespace_order(component_name, account_id):
    """The namespaces to try for a name, most likely first.

    Account components resolve under
    ``arn:aws:greengrass:{region}:{account_id}:components:{name}`` and the
    AWS-managed ones under ``…:aws:components:{name}``. The WRONG namespace
    answers with an EMPTY LIST, not an error (evidence.md §1.1), so both are
    tried and either hit resolves.
    """
    if str(component_name).startswith(AWS_MANAGED_PREFIX):
        order = [AWS_NAMESPACE, account_id]
    else:
        order = [account_id, AWS_NAMESPACE]
    return [namespace for namespace in order if namespace]


def greengrass_fetchers(greengrass_client, region, account_id):
    """``(fetch_recipe, list_versions)`` reading the account through
    ``greengrassv2``, memoized for ONE validation.

    * one ``list_component_versions`` per component name — the namespace it
      resolves in is remembered, so a second namespace is only ever queried
      for a name that resolves in neither;
    * one ``get_component(recipeOutputFormat='JSON')`` per
      ``(name, version)``, in the namespace already established for the name.

    Both may raise; ``resolve_closure`` wraps every call.
    """
    #: name -> ([versions], namespace) or the Exception the lookup raised.
    #: One entry per name means at most ONE ListComponentVersions round per
    #: name for the whole validation, failures included.
    version_cache = {}
    #: (name, version) -> recipe body or the Exception GetComponent raised.
    recipe_cache = {}

    def _load(component_name):
        if component_name in version_cache:
            cached = version_cache[component_name]
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            found = []
            resolved_namespace = None
            for namespace in namespace_order(component_name, account_id):
                versions = []
                arn = component_arn(region, namespace, component_name)
                paginator = greengrass_client.get_paginator(
                    'list_component_versions')
                for page in paginator.paginate(arn=arn):
                    for entry in page.get('componentVersions', []) or []:
                        version = entry.get('componentVersion')
                        if version:
                            versions.append(str(version))
                if versions:
                    # Either namespace hit resolves; the second is only ever
                    # queried for a name that resolves in neither.
                    found = versions
                    resolved_namespace = namespace
                    break
        except Exception as error:                 # noqa: BLE001 — fail open
            version_cache[component_name] = error
            raise
        version_cache[component_name] = (found, resolved_namespace)
        return found, resolved_namespace

    def list_versions(component_name):
        return _load(component_name)[0]

    def fetch_recipe(component_name, component_version):
        key = (component_name, str(component_version))
        if key in recipe_cache:
            cached = recipe_cache[key]
            if isinstance(cached, Exception):
                raise cached
            return cached
        # The namespace is established by the version listing, so the recipe
        # read is ONE call in the namespace that actually publishes the name.
        _versions, namespace = _load(component_name)
        if not namespace:
            error = ValueError(
                f'{component_name} has no published version in either the '
                f'account or the aws namespace')
            recipe_cache[key] = error
            raise error
        try:
            response = greengrass_client.get_component(
                arn=component_arn(region, namespace, component_name,
                                  component_version),
                recipeOutputFormat='JSON')
        except Exception as error:                 # noqa: BLE001 — fail open
            recipe_cache[key] = error
            raise
        recipe = response.get('recipe')
        recipe_cache[key] = recipe
        return recipe

    return fetch_recipe, list_versions
