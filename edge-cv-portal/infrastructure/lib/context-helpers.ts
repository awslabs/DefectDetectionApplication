/**
 * Default-ON resolution for the `deployGroundedSamWorker` CDK context flag
 * (portal-deploy-flag-hardening Req 1). Flag-less deploys deleted the live
 * DdaGroundedSamWorker four times; the safe outcome is now the default one.
 * Only an explicit false — boolean `false` or a string equal to 'false'
 * case-insensitively after trimming — omits the worker (the deliberate
 * teardown path, Req 2.2). Absent, true, 'true', and every unrecognized
 * value (e.g. '0', 'no', typos) deploy it: an unintended deploy is cheap
 * and ECR-cached; an unintended deletion breaks live pre-labeling.
 */
export function groundedSamWorkerEnabled(contextValue: unknown): boolean {
  if (contextValue === false) return false;
  if (
    typeof contextValue === 'string' &&
    contextValue.trim().toLowerCase() === 'false'
  ) {
    return false;
  }
  return true;
}

/**
 * Default-OFF resolution for the `portalRegistryEnforced` CDK context flag
 * (portal-jwt-role-privilege-escalation Req 2.4, design Decision 4). The
 * returned string is what lands in every portal handler's
 * `PORTAL_REGISTRY_ENFORCED` environment variable, where
 * `shared_utils.registry_enforcement_enabled()` reads it.
 *
 * Off is the safe default and must stay the default until the registry has
 * been backfilled from the pool (task 5.2): with the flag on and a row
 * missing, the portal denies that principal everything — including the
 * bootstrap `admin`. Only an explicit affirmative turns it on: boolean
 * `true`, or a string that is one of '1'/'true'/'yes'/'on'/'enabled' after
 * trimming, case-insensitively — exactly the truthy set
 * `shared_utils._ENFORCEMENT_TRUE_VALUES` accepts, so a value that reads as
 * "on" in CDK context can never deploy as "off" in the Lambda (or the
 * reverse). Absent and every unrecognized value ('0', 'no', 'maybe', typos)
 * resolve to 'false'.
 *
 * The value is normalized to the canonical 'true'/'false' rather than passed
 * through, so the deployed environment is unambiguous when read from the
 * console or a CloudFormation template.
 */
export function portalRegistryEnforced(contextValue: unknown): string {
  if (contextValue === true) return 'true';
  if (
    typeof contextValue === 'string' &&
    ['1', 'true', 'yes', 'on', 'enabled'].includes(
      contextValue.trim().toLowerCase(),
    )
  ) {
    return 'true';
  }
  return 'false';
}

/**
 * Default-ON resolution for the `deployMcpServer` CDK context flag
 * (mcp-servers design §1, D33). Same rule as
 * {@link groundedSamWorkerEnabled}: only an explicit false (boolean `false`,
 * or a string equal to 'false' case-insensitively after trimming) omits
 * EdgeCVPortalMcpStack; absent, true and every other value ('0', 'no', 'off',
 * typos) synthesize it. deploy-infrastructure.sh's opt-out guard applies the
 * same test to DEPLOY_MCP_SERVER, so the shell and CDK rules cannot differ.
 */
export function mcpServerEnabled(contextValue: unknown): boolean {
  return groundedSamWorkerEnabled(contextValue);
}

/**
 * Default-OFF resolution for the `mcpMutatingToolsEnabled` CDK context flag
 * (mcp-servers design D9). The returned string is the MCP Lambda's
 * `MCP_MUTATING_TOOLS_ENABLED`: 'true' only for boolean `true` or one of
 * '1'/'true'/'yes'/'on'/'enabled' after trimming, case-insensitively (the
 * {@link portalRegistryEnforced} truthy set); 'false' for absent and every
 * other value. Always one of the two canonical strings.
 */
export function mcpMutatingToolsEnabled(contextValue: unknown): string {
  return portalRegistryEnforced(contextValue);
}

/**
 * Default-ON resolution for the `deployAssistant` CDK context flag
 * (portal-ai-assistant design D8): the same rule as {@link mcpServerEnabled}
 * - only an explicit false omits EdgeCVPortalAssistantStack, and
 * deploy-infrastructure.sh's guard applies the same test to DEPLOY_ASSISTANT.
 */
export function assistantEnabled(contextValue: unknown): boolean {
  return groundedSamWorkerEnabled(contextValue);
}
/**
 * Default-ON resolution for the `enableRfDetrSeg` CDK context flag — the
 * Seg_Switch (rfdetr-seg-support Requirement 15.1, design DD7, owner
 * decision O12 b). Same rule as {@link groundedSamWorkerEnabled}: only an
 * explicit false (boolean `false`, or a string equal to 'false'
 * case-insensitively after trimming) turns the RF-DETR-Seg feature off;
 * absent, true and every other value ('0', 'no', 'off', typos) leave it on.
 * The compute stack renders the result as the canonical 'true' / 'false'
 * string in `PORTAL_RFDETR_SEG_ENABLED` on exactly the training, model
 * converter and packaging handlers (never the shared lambdaEnvironment) and
 * as the `RfDetrSegEnabled` stack output that deploy-frontend.sh copies
 * into config.json `rfDetrSegEnabled`, where the frontend applies the same
 * absent-means-on rule.
 */
export function rfDetrSegEnabled(contextValue: unknown): boolean {
  return groundedSamWorkerEnabled(contextValue);
}

/**
 * Default-OFF resolution for the `buildFleetNatGateway` CDK context flag, the
 * Build Fleet's optional NAT gateway (lib/build-fleet-stack.ts). On only for
 * boolean `true` or one of '1'/'true'/'yes'/'on'/'enabled' after trimming,
 * case-insensitively: the {@link portalRegistryEnforced} truthy set, which
 * scripts/portal-build-fleet-nat.sh also applies to BUILD_FLEET_NAT_GATEWAY.
 * Off for absent and every other value, because the gateway is billed by the
 * hour whether builds run or not.
 */
export function buildFleetNatGatewayEnabled(contextValue: unknown): boolean {
  return portalRegistryEnforced(contextValue) === 'true';
}

// ---------------------------------------------------------------------------
// Synth-time enforcement acknowledgement (portal-authorization-and-iam-
// scoping, design I7 "Design (D7-A)"). bin/app.ts refuses to synthesize when
// `portalRegistryEnforced` is absent or unrecognized and the fresh-install
// acknowledgement `allowLegacyRoles` is not given, because
// portalRegistryEnforced() resolves both to 'false': a bare `cdk deploy`, or a
// typo such as 'ture', would otherwise turn enforcement off unannounced.
// ---------------------------------------------------------------------------

/**
 * The spellings that name enforcement off explicitly, beside the on set of
 * {@link portalRegistryEnforced} ('1'/'true'/'yes'/'on'/'enabled'); both are
 * compared after trimming, case-insensitively.
 */
export const ENFORCEMENT_OFF_VALUES = ['0', 'false', 'no', 'off', 'disabled'];

/** True for boolean `true` or a string in portalRegistryEnforced()'s on set. */
function isOnValue(value: unknown): boolean {
  return portalRegistryEnforced(value) === 'true';
}

/**
 * How the `portalRegistryEnforced` context value reads: 'absent' (undefined,
 * null, or a string that is empty after trimming, so
 * `-c portalRegistryEnforced=` is absent), 'recognized' (a boolean, or a
 * string in the on set or {@link ENFORCEMENT_OFF_VALUES}) or 'unrecognized'
 * (anything else).
 */
export function enforcementContextKind(enforcedCtx: unknown): 'absent' | 'recognized' | 'unrecognized' {
  if (enforcedCtx === undefined || enforcedCtx === null) return 'absent';
  if (typeof enforcedCtx === 'boolean') return 'recognized';
  if (typeof enforcedCtx !== 'string') return 'unrecognized';
  const normalized = enforcedCtx.trim().toLowerCase();
  if (normalized.length === 0) return 'absent';
  if (isOnValue(normalized) || ENFORCEMENT_OFF_VALUES.includes(normalized)) return 'recognized';
  return 'unrecognized';
}

/**
 * True iff the app may synthesize: `portalRegistryEnforced` is recognized, or
 * the `allowLegacyRoles` acknowledgement is truthy (the on set). The deploy
 * scripts pass the acknowledgement only on a fresh install, where no portal
 * handler is deployed yet and the CDK default, off, is the intended value.
 */
export function legacyRolesAcknowledged(enforcedCtx: unknown, ack: unknown): boolean {
  return enforcementContextKind(enforcedCtx) === 'recognized' || isOnValue(ack);
}

/**
 * The message bin/app.ts throws when {@link legacyRolesAcknowledged} refuses:
 * two sentences naming the ways forward, plus a third naming the value for an
 * unrecognized one. Branches on {@link enforcementContextKind}, so the
 * predicate and the message cannot disagree.
 */
export function enforcementRefusalMessage(enforcedCtx: unknown): string {
  const remedy =
    'portalRegistryEnforced is unset. Pass -c portalRegistryEnforced=true|false (deploy scripts ' +
    'resolve this from the deployed value) or -c allowLegacyRoles=true to deploy a fresh install ' +
    'with enforcement off.';
  if (enforcementContextKind(enforcedCtx) !== 'unrecognized') return remedy;
  const value = typeof enforcedCtx === 'string' ? enforcedCtx : JSON.stringify(enforcedCtx);
  return (
    `${remedy} \`portalRegistryEnforced=${value}\` is not a recognized spelling; use one of ` +
    '1/true/yes/on/enabled or 0/false/no/off/disabled.'
  );
}

/**
 * Normalizes the `cloudFrontDomain` CDK context value to the bare domain
 * (portal-deploy-flag-hardening Req 3). Consumers prepend the scheme
 * themselves (storage-stack CORS `https://${…}`, backend link/CORS
 * builders), so a scheme-prefixed context value produced `https://https://…`
 * live on 2026-09-07. Strips surrounding whitespace, at most one leading
 * `http://`/`https://` (case-insensitive), and all trailing slashes; every
 * other character passes through unchanged. Returns undefined when the
 * input is absent or empty after normalization, preserving today's
 * absent-context behavior (wildcard CORS, no env entries).
 */
export function normalizeCloudFrontDomain(
  contextValue: unknown,
): string | undefined {
  if (typeof contextValue !== 'string') return undefined;
  let domain = contextValue.trim();
  domain = domain.replace(/^https?:\/\//i, '');
  domain = domain.replace(/\/+$/, '');
  return domain.length > 0 ? domain : undefined;
}
/**
 * Resolves the `detectorExportImage` CDK context value
 * (detector-checkpoint-import design D2). This is the Export_Image a
 * Conversion_Job runs. It lands in ModelConverterHandler's
 * `DETECTOR_EXPORT_IMAGE`, and every Conversion_Record carries it.
 *
 * - Absent or blank: returns '' (conversion is not configured). Convert then
 *   returns 503 and inspect reports `convertible: false` with that reason
 *   (Requirement 4.8).
 * - Otherwise the value MUST be an ECR image pinned by digest,
 *   `<account>.dkr.ecr.<region>.amazonaws.com/<repository>@sha256:<64 hex>`.
 *   A tag is mutable, so it cannot be the image a record says it ran; any
 *   other value fails the synth.
 */
export function detectorExportImage(contextValue: unknown): string {
  if (contextValue === undefined || contextValue === null) return '';
  if (typeof contextValue !== 'string') {
    throw new Error(
      `detectorExportImage must be a string (an ECR image URI pinned by digest); got ${typeof contextValue}`,
    );
  }
  const uri = contextValue.trim();
  if (uri.length === 0) return '';
  const pinned =
    /^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(\.cn)?\/[a-z0-9]+(?:[._\/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$/;
  if (!pinned.test(uri)) {
    throw new Error(
      'detectorExportImage must be an ECR image pinned by digest ' +
        '(<account>.dkr.ecr.<region>.amazonaws.com/<repository>@sha256:<digest>, as ' +
        `detector-export-image/build-and-push.sh --push prints); got ${uri}`,
    );
  }
  return uri;
}

/** SSM parameter bin/app.ts falls back to for `detectorExportImage`. */
export const DETECTOR_EXPORT_IMAGE_SSM_PARAMETER = '/dda-portal/detector-export-image';

/**
 * The `authorizerName` of the portal REST API's own Cognito authorizer
 * (lib/api-gateway-stack.ts `CognitoAuthorizer`). deploy-infrastructure.sh
 * resolves {@link SHARED_AUTHORIZER_CONTEXT_KEY} by filtering
 * `aws apigateway get-authorizers` on this name.
 */
export const PORTAL_API_AUTHORIZER_NAME = 'EdgeCVPortalAuthorizer';

/**
 * CDK context key carrying the id of an existing Cognito authorizer on the
 * portal REST API (`-c sharedAuthorizerId=<id>`).
 */
export const SHARED_AUTHORIZER_CONTEXT_KEY = 'sharedAuthorizerId';

/**
 * The `sharedAuthorizerId` CDK context value when one is given: the trimmed
 * string for a non-blank value, undefined when the key is absent or blank.
 * bin/app.ts calls this once and instantiates the WorkflowDebugStack and the
 * TelemetryStack only when it returns a value, so every other stack
 * synthesizes and deploys as before without the key (deploy-auth.sh,
 * deploy-frontend.sh and deploy_portal_fixes.sh pass no such context, and a
 * fresh install has no API to look the id up on yet). No validation here:
 * the stacks receive the value as a prop and {@link sharedAuthorizerId}
 * rejects a malformed one at synth.
 */
export function sharedAuthorizerIdIfGiven(contextValue: unknown): string | undefined {
  if (contextValue === undefined || contextValue === null) return undefined;
  const value = String(contextValue).trim();
  return value.length > 0 ? value : undefined;
}

/**
 * Validates the `sharedAuthorizerId` prop of the WorkflowDebugStack and the
 * TelemetryStack: the id of an existing Cognito authorizer on the portal
 * REST API that both attach their methods to instead of creating one each.
 *
 * API Gateway allows 10 authorizers per REST API, a quota that only AWS
 * Support raises, and the portal API already carries one per
 * route-registering stack; the first deploy of a stack that created its own
 * rolled back on that limit on 2026-10-18. The value is deploy-time input:
 * deploy-infrastructure.sh reads the Compute stack's `ApiId` output, filters
 * `aws apigateway get-authorizers` on {@link PORTAL_API_AUTHORIZER_NAME}
 * and passes `-c sharedAuthorizerId=<id>`; an `--exclusively` deploy passes
 * it by hand. bin/app.ts hands the context value to the two stacks as a
 * prop ({@link sharedAuthorizerIdIfGiven}) and skips them when it is absent.
 *
 * Absent, blank and malformed values throw at synth, naming `consumer`:
 * there is no safe default. A made-up id deploys methods nobody can call,
 * and creating an authorizer is the failure this replaces. Anything else is
 * trimmed and must look like an API Gateway id (lowercase letters and
 * digits), so a lookup that printed an error or several ids fails here
 * rather than in CloudFormation.
 */
export function sharedAuthorizerId(contextValue: unknown, consumer: string): string {
  const value =
    contextValue === undefined || contextValue === null ? '' : String(contextValue).trim();
  if (value.length === 0) {
    throw new Error(
      `${consumer} requires ${SHARED_AUTHORIZER_CONTEXT_KEY}: the id of an existing Cognito ` +
        'authorizer on the portal REST API. bin/app.ts passes the CDK context value ' +
        '(-c sharedAuthorizerId=<id>, which deploy-infrastructure.sh resolves from ' +
        `\`aws apigateway get-authorizers\` by the name ${PORTAL_API_AUTHORIZER_NAME}) and ` +
        'skips the stack when it is absent. Refusing to synth: the API allows 10 ' +
        'authorizers and the stack never creates one of its own.',
    );
  }
  if (!/^[a-z0-9]+$/.test(value)) {
    throw new Error(
      `${SHARED_AUTHORIZER_CONTEXT_KEY} must be an API Gateway authorizer id ` +
        `(lowercase letters and digits); got ${value}`,
    );
  }
  return value;
}

/**
 * The default `detectorExportImage` context bin/app.ts hands to the App
 * (detector-checkpoint-import task 10). This follows the trusted-account
 * precedent: env `DETECTOR_EXPORT_IMAGE` first, then the SSM parameter
 * {@link DETECTOR_EXPORT_IMAGE_SSM_PARAMETER}.
 *
 * App-props context is overridden by every CLI or cdk.json value, so
 * `-c detectorExportImage=...` still wins. `-c detectorExportImage=` (blank)
 * still disables conversion. What this adds: a routine deploy that passes no
 * `-c` at all (deploy-infrastructure.sh, and deploy-frontend.sh's compute
 * redeploy) keeps the configured image instead of silently resetting it and
 * turning every convert into a 503.
 *
 * Returns undefined when neither source has a value. A failed SSM read (no
 * parameter, no credentials) also counts as no value. Validation stays with
 * {@link detectorExportImage}, so a bad value from either source still fails
 * the synth.
 */
export function detectorExportImageDefault(
  env: Record<string, string | undefined>,
  readSsmParameter: (name: string) => string | undefined,
): string | undefined {
  const fromEnv = (env.DETECTOR_EXPORT_IMAGE ?? '').trim();
  if (fromEnv) return fromEnv;
  let fromSsm = '';
  try {
    fromSsm = (readSsmParameter(DETECTOR_EXPORT_IMAGE_SSM_PARAMETER) ?? '').trim();
  } catch {
    return undefined;
  }
  return fromSsm && fromSsm !== 'None' ? fromSsm : undefined;
}

// ---------------------------------------------------------------------------
// Per-deployment LocalServer feature floors (car-assembly-check design §7,
// DD9). Greengrass component versions are per account, so a floor is
// configuration of the deployment, not a literal of the tree: each floor is a
// CDK context key whose default comes from a shell variable, then an SSM
// parameter, and whose value is a {workflow_core arch id: 'N.N.N'} map.
// ---------------------------------------------------------------------------

/** The workflow_core arch ids a floor map may name (the packager's
 *  ARCH_TO_LOCAL_SERVER_COMPONENT keys; pinned by the backend test
 *  test_layout_check_feature_floor_coverage.py). */
export const MIN_VERSIONS_FLOOR_ARCHES = [
  'arm64_cpu',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
  'x86_64',
  'x86_64_nvidia',
];

/** SSM parameter bin/app.ts falls back to for `layoutCheckMinLocalServerVersions`. */
export const LAYOUT_CHECK_FLOOR_SSM_PARAMETER = '/dda-portal/layout-check-min-local-server-versions';

/** SSM parameter bin/app.ts falls back to for `rfdetrSegMinLocalServerVersions`. */
export const RFDETR_SEG_FLOOR_SSM_PARAMETER = '/dda-portal/rfdetr-seg-min-local-server-versions';

/**
 * The default context value of a per-deployment floor key: the shell variable
 * `envVar` first, else the SSM parameter `ssmParameter`, else undefined (the
 * {@link detectorExportImageDefault} shape: trimmed; a blank value, a failed
 * read and the CLI's literal `None` count as no value). bin/app.ts's
 * `readSsmParameter` reads the parameter in
 * `CDK_DEFAULT_REGION || AWS_REGION || AWS_DEFAULT_REGION || 'us-east-1'`, so
 * the fallback is the deploying account's parameter in the region the deploy
 * resolves. The value is not validated here: {@link parseMinVersionsFloor}
 * validates whichever source wins.
 */
export function minVersionsFloorDefault(
  envVar: string,
  ssmParameter: string,
  env: Record<string, string | undefined>,
  readSsmParameter: (name: string) => string | undefined,
): string | undefined {
  const fromEnv = (env[envVar] ?? '').trim();
  if (fromEnv) return fromEnv;
  let fromSsm = '';
  try {
    fromSsm = (readSsmParameter(ssmParameter) ?? '').trim();
  } catch {
    return undefined;
  }
  return fromSsm && fromSsm !== 'None' ? fromSsm : undefined;
}

/**
 * Validates a per-deployment floor context value. `undefined`, `null` and a
 * string that is blank after trim() are absent (undefined). Any other string
 * is JSON text and an object is taken as is; the result must be a JSON object
 * whose every key is one of {@link MIN_VERSIONS_FLOOR_ARCHES} and every value
 * matches `N.N.N`. Anything else throws, naming the context key, the
 * offending entry and the SSM parameter, so a malformed floor fails the
 * synth instead of silently failing closed or open.
 */
export function parseMinVersionsFloor(
  contextKey: string,
  ssmParameter: string,
  value: unknown,
): Record<string, string> | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'string' && value.trim().length === 0) return undefined;
  const fail = (entry: string): never => {
    throw new Error(
      `${contextKey} must be a JSON object of {arch: 'N.N.N'} with arch one of ` +
        `${MIN_VERSIONS_FLOOR_ARCHES.join(', ')} (set with -c ${contextKey}=... or the ` +
        `SSM parameter ${ssmParameter}); offending entry: ${entry}`,
    );
  };
  let parsed: unknown = value;
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value);
    } catch {
      return fail(value);
    }
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return fail(typeof value === 'string' ? value : JSON.stringify(value));
  }
  const floor: Record<string, string> = {};
  for (const [arch, version] of Object.entries(parsed as Record<string, unknown>)) {
    if (!MIN_VERSIONS_FLOOR_ARCHES.includes(arch)) fail(`${arch}: ${JSON.stringify(version)}`);
    if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) {
      fail(`${arch}: ${JSON.stringify(version)}`);
    }
    floor[arch] = version as string;
  }
  return floor;
}

/** The default `layoutCheckMinLocalServerVersions` context: env
 *  `LAYOUT_CHECK_FLOOR` (distinct from the Lambda variable, so a developer's
 *  shell cannot leak into the infra tests), then
 *  {@link LAYOUT_CHECK_FLOOR_SSM_PARAMETER}. */
export function layoutCheckFloorDefault(
  env: Record<string, string | undefined>,
  readSsmParameter: (name: string) => string | undefined,
): string | undefined {
  return minVersionsFloorDefault('LAYOUT_CHECK_FLOOR', LAYOUT_CHECK_FLOOR_SSM_PARAMETER, env, readSsmParameter);
}

/** The Layout_Check floor map; absent (or blank) is `{}`: no floor, so the
 *  packager and the deployment gate refuse every Layout_Check workflow. */
export function parseLayoutCheckFloor(value: unknown): Record<string, string> {
  return parseMinVersionsFloor('layoutCheckMinLocalServerVersions', LAYOUT_CHECK_FLOOR_SSM_PARAMETER, value) ?? {};
}

/** The default `rfdetrSegMinLocalServerVersions` context: env
 *  `RFDETR_SEG_FLOOR`, then {@link RFDETR_SEG_FLOOR_SSM_PARAMETER}. */
export function rfDetrSegFloorDefault(
  env: Record<string, string | undefined>,
  readSsmParameter: (name: string) => string | undefined,
): string | undefined {
  return minVersionsFloorDefault('RFDETR_SEG_FLOOR', RFDETR_SEG_FLOOR_SSM_PARAMETER, env, readSsmParameter);
}

// ---------------------------------------------------------------------------
// S3 data-plane scoping of the portal Lambda roles
// (portal-authorization-and-iam-scoping, design I5 "Design D5-B").
//
// One helper builds the S3 data-plane statements of the three stacks that
// grant them (compute-stack.ts createLambdaRole, synthetic-data-stack.ts,
// node-designer-stack.ts), so the three cannot diverge:
//
//   'tag'              the bucket-level statement (s3:ListBucket,
//                      s3:GetBucketLocation on arn:aws:s3:::*) and the
//                      object-level statements (the objects of every bucket,
//                      or one key prefix of every bucket) carry
//                      PORTAL_MANAGED_TAG_CONDITION; the bucket allowlist is
//                      ignored.
//   'tag-objects-only' the object-level statements carry the condition; the
//                      bucket-level statement names the allowlist's buckets.
//   'allowlist'        both name the allowlist, unconditioned (design
//                      Appendix A; owner gate G1 only).
//
// In every scoping the tag-discovery statement (s3:GetBucketTagging,
// s3:GetBucketABAC on arn:aws:s3:::*) stays unconditioned: the eligibility
// read and the ABAC-status read run on a tagged bucket before its ABAC is
// enabled, where a tag-conditioned statement could not match. Neither read
// returns object data or a listing.
// ---------------------------------------------------------------------------

export type DataPlaneScoping = 'tag' | 'tag-objects-only' | 'allowlist';

// Set from the live probe's decision rule (task 5.3). 'tag' is the design's
// expected outcome; it was written before the probe ran, and the deploy gate
// (task 9.2) requires the probe to have confirmed it, or the post-probe code
// change to be applied, before any deploy. The compute stack carries the
// value into every handler's DATA_PLANE_SCOPING, which the shared layer reads
// through data_plane_scoping(); the deploy scripts log it after their build.
export const DATA_PLANE_SCOPING: DataPlaneScoping = 'tag';

// The Tag_Condition. S3 evaluates bucket-tag condition keys only on a bucket
// whose ABAC is enabled, so a statement carrying it matches a bucket tagged
// dda-portal:managed=true with ABAC on, and no other bucket.
export const PORTAL_MANAGED_TAG_CONDITION = {
  StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' },
};

/** The resources and the optional condition of one data-plane statement. */
export interface DataPlaneScope {
  resources: string[];
  conditions?: { [operator: string]: { [key: string]: string } };
}

/** A data-plane statement with its fixed action set. */
export interface DataPlaneStatement extends DataPlaneScope {
  actions: string[];
}

export interface DataPlaneGrant {
  /** `s3:ListBucket` and `s3:GetBucketLocation`. */
  bucketLevel: DataPlaneStatement;
  /** `s3:GetObject` and `s3:PutObject`. */
  objectLevel: DataPlaneStatement;
  /** The object-level scope confined to one key prefix; the caller names the actions. */
  objectPrefix(prefix: string): DataPlaneScope;
  /** `s3:GetBucketTagging` and `s3:GetBucketABAC` on every bucket, unconditioned in every scoping. */
  tagDiscovery: DataPlaneStatement;
}

const ANY_BUCKET_ARN = 'arn:aws:s3:::*';
const BUCKET_LEVEL_ACTIONS = ['s3:ListBucket', 's3:GetBucketLocation'];
const OBJECT_LEVEL_ACTIONS = ['s3:GetObject', 's3:PutObject'];
const TAG_DISCOVERY_ACTIONS = ['s3:GetBucketTagging', 's3:GetBucketABAC'];

// A fresh copy per statement, so no statement shares a condition object.
function portalManagedTagCondition() {
  return { StringEquals: { ...PORTAL_MANAGED_TAG_CONDITION.StringEquals } };
}

// The canonical bucket ARN of an allowlist entry: a bare bucket name, or a
// bucket ARN with an optional trailing '/' or '/' plus '*'.
function canonicalBucketArn(entry: string): string {
  return entry.startsWith('arn:aws:s3:::') ? entry.replace(/\/\*?$/, '') : `arn:aws:s3:::${entry}`;
}

/**
 * The S3 data-plane statements of a portal Lambda role under `scoping`
 * (default {@link DATA_PLANE_SCOPING}). `allowlist` is the
 * `-c dataBucketAllowlist` value (bucket names or ARNs); it is ignored under
 * 'tag' and must be non-empty under the other two scopings, which throw
 * rather than fall back to every bucket. Pure.
 */
export function dataPlaneGrant(
  allowlist: string[] | undefined,
  scoping: DataPlaneScoping = DATA_PLANE_SCOPING,
): DataPlaneGrant {
  const tagDiscovery: DataPlaneStatement = {
    actions: [...TAG_DISCOVERY_ACTIONS],
    resources: [ANY_BUCKET_ARN],
  };
  const conditioned = (resources: string[]): DataPlaneScope => ({
    resources,
    conditions: portalManagedTagCondition(),
  });
  const conditionedObjects = {
    objectLevel: { actions: [...OBJECT_LEVEL_ACTIONS], ...conditioned([`${ANY_BUCKET_ARN}/*`]) },
    objectPrefix: (prefix: string) => conditioned([`${ANY_BUCKET_ARN}/${prefix}/*`]),
  };
  if (scoping === 'tag') {
    return {
      bucketLevel: { actions: [...BUCKET_LEVEL_ACTIONS], ...conditioned([ANY_BUCKET_ARN]) },
      ...conditionedObjects,
      tagDiscovery,
    };
  }
  const bucketArns = (allowlist ?? [])
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0)
    .map(canonicalBucketArn);
  if (bucketArns.length === 0) {
    throw new Error(
      `Data_Plane_Scoping '${scoping}' names its buckets and needs a non-empty bucket allowlist ` +
        '(-c dataBucketAllowlist=<bucket>,<bucket>); it never falls back to every bucket.',
    );
  }
  const bucketLevel = { actions: [...BUCKET_LEVEL_ACTIONS], resources: bucketArns };
  if (scoping === 'tag-objects-only') {
    return { bucketLevel, ...conditionedObjects, tagDiscovery };
  }
  return {
    bucketLevel,
    objectLevel: { actions: [...OBJECT_LEVEL_ACTIONS], resources: bucketArns.map((arn) => `${arn}/*`) },
    objectPrefix: (prefix: string) => ({ resources: bucketArns.map((arn) => `${arn}/${prefix}/*`) }),
    tagDiscovery,
  };
}
