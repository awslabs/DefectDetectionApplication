/**
 * Route_Auth_Inventory (portal-authorization-and-iam-scoping, design I3
 * "Route inventory"; task 3.3; Requirements 5.4, 5.5).
 *
 * One committed statement of where the portal registers API Gateway methods
 * and which of them run without the Cognito authorizer:
 *
 *   - API_STACK_INVENTORY: the six top-level stacks that register methods
 *     on the portal REST API, with the nested API stacks (by construct id)
 *     and the lib/ files whose code registers the methods;
 *   - COMMITTED_NONE_METHODS: the methods that are deliberately
 *     AuthorizationType NONE, each with the control that replaces the
 *     authorizer;
 *   - UNINSTANTIATED_API_FILES: the three lib/ files that register methods
 *     but that no app entry point or stack instantiates.
 *
 * The collector, the CORS preflight partition, the pure predicate and the
 * ROUTE_AUTH.md renderer live here so that scripts/generate-route-auth.ts
 * (which writes ROUTE_AUTH.md) and test/route-auth-inventory.test.ts (which
 * compares the committed file with a fresh rendering) share one
 * implementation. The universe is the set of synthesized
 * AWS::ApiGateway::Method resources, not source call sites.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';

/** The authorization type every portal method carries unless committed below. */
export const COGNITO_AUTHORIZATION = 'COGNITO_USER_POOLS';

/** One place in the source where methods are registered. */
export interface MethodSource {
  /** Construct id of the nested API stack, or null for methods registered on the top-level stack itself. */
  readonly nestedStackId: string | null;
  /** The lib/ file whose code registers the methods. */
  readonly file: string;
}

/** A top-level stack that registers methods on the portal REST API. */
export interface InventoryStack {
  /** The stack id bin/app.ts gives the stack. */
  readonly stackName: string;
  /** How the stack registers its methods. */
  readonly registration: 'nested' | 'direct';
  readonly sources: ReadonlyArray<MethodSource>;
}

export const API_STACK_INVENTORY: ReadonlyArray<InventoryStack> = [
  {
    stackName: 'EdgeCVPortalComputeStack',
    registration: 'nested',
    sources: [
      { nestedStackId: 'ApiGateway', file: 'api-gateway-stack.ts' },
      { nestedStackId: 'CameraRegistryApi', file: 'camera-registry-api-stack.ts' },
      { nestedStackId: 'UserAdminApi', file: 'user-admin-api-stack.ts' },
      { nestedStackId: 'QuickSetupApi', file: 'quick-setup-api-stack.ts' },
      { nestedStackId: 'DdaLabelingApi', file: 'dda-labeling-api-stack.ts' },
      { nestedStackId: 'WorkflowManagerGapsApi', file: 'workflow-manager-gaps-api-stack.ts' },
      { nestedStackId: 'WorkflowTuningApi', file: 'workflow-tuning-api-stack.ts' },
    ],
  },
  {
    stackName: 'EdgeCVPortalNodeDesignerStack',
    registration: 'nested',
    sources: [{ nestedStackId: 'NodeDesignerApi', file: 'node-designer-api-stack.ts' }],
  },
  {
    stackName: 'EdgeCVPortalBuildFleetStack',
    registration: 'direct',
    sources: [{ nestedStackId: null, file: 'build-fleet-stack.ts' }],
  },
  {
    stackName: 'EdgeCVPortalSyntheticDataStack',
    registration: 'direct',
    sources: [{ nestedStackId: null, file: 'synthetic-data-stack.ts' }],
  },
  // The two stacks bin/app.ts instantiates only when the sharedAuthorizerId
  // context is given (workflow-debug-mode, workflow-telemetry-dashboards);
  // their methods attach to the portal API's existing Cognito authorizer by id.
  {
    stackName: 'EdgeCVPortalWorkflowDebugStack',
    registration: 'direct',
    sources: [{ nestedStackId: null, file: 'workflow-debug-stack.ts' }],
  },
  {
    stackName: 'EdgeCVPortalTelemetryStack',
    registration: 'direct',
    sources: [{ nestedStackId: null, file: 'telemetry-stack.ts' }],
  },
];

/** A method that is deliberately registered without the Cognito authorizer. */
export interface CommittedNoneMethod {
  readonly stackName: string;
  readonly httpMethod: string;
  readonly path: string;
  /** The control that stands in for the authorizer. */
  readonly control: string;
}

const QUICK_SETUP_CONTROL =
  'Setup_Token validated in the handler (quick_setup.py), an invalid-token limiter per source address, and method-level throttling';

export const COMMITTED_NONE_METHODS: ReadonlyArray<CommittedNoneMethod> = [
  {
    stackName: 'EdgeCVPortalComputeStack',
    httpMethod: 'POST',
    path: '/auth/refresh',
    control: 'Token refresh: the caller presents a Cognito refresh token, which the handler exchanges with Cognito',
  },
  {
    stackName: 'EdgeCVPortalComputeStack',
    httpMethod: 'GET',
    path: '/quick-setup/bootstrap',
    control: 'Public bootstrap script (no portal data); method-level throttling',
  },
  {
    stackName: 'EdgeCVPortalComputeStack',
    httpMethod: 'POST',
    path: '/quick-setup/bundle',
    control: QUICK_SETUP_CONTROL,
  },
  {
    stackName: 'EdgeCVPortalComputeStack',
    httpMethod: 'POST',
    path: '/quick-setup/credentials',
    control: QUICK_SETUP_CONTROL,
  },
  {
    stackName: 'EdgeCVPortalComputeStack',
    httpMethod: 'POST',
    path: '/quick-setup/status',
    control: QUICK_SETUP_CONTROL,
  },
];

/** lib/ files that register methods but are not instantiated by any app entry point or stack. */
export const UNINSTANTIATED_API_FILES: ReadonlyArray<{ readonly file: string; readonly className: string }> = [
  { file: 'api-core-stack.ts', className: 'ApiCoreStack' },
  { file: 'api-data-stack.ts', className: 'ApiDataStack' },
  { file: 'api-model-stack.ts', className: 'ApiModelStack' },
];

/** One synthesized AWS::ApiGateway::Method resource. */
export interface CollectedMethod {
  readonly stackName: string;
  /** Construct id of the nested stack that holds the resource, or null for the top-level template. */
  readonly nestedStackId: string | null;
  readonly logicalId: string;
  readonly httpMethod: string;
  readonly path: string;
  readonly authorizationType: string;
  readonly integrationType: string;
}

/** Key a method by stack, verb and path (the identity the committed NONE set uses). */
export function methodKey(m: { stackName: string; httpMethod: string; path: string }): string {
  return `${m.stackName} ${m.httpMethod} ${m.path}`;
}

/** CORS_Preflight_Method: an OPTIONS method with a MOCK integration (emitted by defaultCorsPreflightOptions). */
export function isCorsPreflight(m: Pick<CollectedMethod, 'httpMethod' | 'integrationType'>): boolean {
  return m.httpMethod === 'OPTIONS' && m.integrationType === 'MOCK';
}

/** Split methods into routes and CORS preflight methods; an OPTIONS method with any other integration stays a route. */
export function partitionPreflight<T extends Pick<CollectedMethod, 'httpMethod' | 'integrationType'>>(
  methods: ReadonlyArray<T>,
): { routes: T[]; preflight: T[] } {
  const routes: T[] = [];
  const preflight: T[] = [];
  for (const m of methods) {
    (isCorsPreflight(m) ? preflight : routes).push(m);
  }
  return { routes, preflight };
}

/**
 * The pure predicate. Returns one message per violation (empty when the
 * method set matches the committed inventory):
 *   - a non-preflight method that is not COGNITO_USER_POOLS and not in the
 *     committed NONE set (or is in it with an authorization type other than NONE);
 *   - a committed NONE method that is absent from the collected methods.
 */
export function routeAuthViolations(
  methods: ReadonlyArray<CollectedMethod>,
  committed: ReadonlyArray<CommittedNoneMethod> = COMMITTED_NONE_METHODS,
): string[] {
  const committedKeys = new Set(committed.map(methodKey));
  const seen = new Set<string>();
  const violations: string[] = [];
  for (const m of partitionPreflight(methods).routes) {
    const key = methodKey(m);
    if (committedKeys.has(key)) {
      seen.add(key);
      if (m.authorizationType !== 'NONE') {
        violations.push(`${key}: committed as NONE but synthesized as ${m.authorizationType}`);
      }
      continue;
    }
    if (m.authorizationType !== COGNITO_AUTHORIZATION) {
      violations.push(`${key}: ${m.authorizationType} is not in the committed NONE set`);
    }
  }
  for (const key of committedKeys) {
    if (!seen.has(key)) {
      violations.push(`${key}: committed NONE method not synthesized`);
    }
  }
  return violations;
}

/** The template a stack synthesizes (top-level or nested), read from the app's assembly. */
function templateOf(stack: cdk.Stack): any {
  const stage = cdk.Stage.of(stack);
  if (!stage) {
    throw new Error(`stack ${stack.node.path} is not part of an app`);
  }
  const assembly = stage.synth();
  if (stack.nestedStackParent) {
    return JSON.parse(fs.readFileSync(path.join(assembly.directory, stack.templateFile), 'utf-8'));
  }
  return assembly.getStackArtifact(stack.artifactId).template;
}

/** Every nested stack below a stack (any depth). */
function nestedStacksOf(stack: cdk.Stack): cdk.NestedStack[] {
  return stack.node
    .findAll()
    .filter((c): c is cdk.NestedStack => c !== stack && cdk.NestedStack.isNestedStack(c));
}

/** Collect every synthesized Method resource of one top-level stack and its nested stacks. */
export function collectStackMethods(stackName: string, stack: cdk.Stack): CollectedMethod[] {
  const out: CollectedMethod[] = [];
  for (const holder of [stack as cdk.Stack, ...nestedStacksOf(stack)]) {
    const template = templateOf(holder);
    // Resolve each method's path from its construct: the L2 Method knows the
    // full resource path even when the parent resource is imported
    // (RestApi.fromRestApiAttributes / Resource.fromResourceAttributes).
    const pathByLogicalId = new Map<string, string>();
    for (const c of holder.node.findAll()) {
      if (c instanceof apigateway.CfnMethod && cdk.Stack.of(c) === holder) {
        const scope = c.node.scope;
        if (scope instanceof apigateway.Method) {
          pathByLogicalId.set(holder.resolve(c.logicalId), scope.resource.path);
        }
      }
    }
    const nestedStackId = holder === stack ? null : holder.node.id;
    for (const [logicalId, res] of Object.entries(template.Resources ?? {}) as [string, any][]) {
      if (res.Type !== 'AWS::ApiGateway::Method') continue;
      const props = res.Properties ?? {};
      out.push({
        stackName,
        nestedStackId,
        logicalId,
        httpMethod: String(props.HttpMethod),
        path: pathByLogicalId.get(logicalId) ?? '<unresolved>',
        authorizationType: String(props.AuthorizationType ?? 'NONE'),
        integrationType: String(props.Integration?.Type ?? '<none>'),
      });
    }
  }
  return out;
}

/** Collect the methods of every inventory stack; `stacks` maps each inventory stack name to its constructed stack. */
export function collectInventoryMethods(
  stacks: Readonly<Record<string, cdk.Stack>>,
  inventory: ReadonlyArray<InventoryStack> = API_STACK_INVENTORY,
): CollectedMethod[] {
  const out: CollectedMethod[] = [];
  for (const entry of inventory) {
    const stack = stacks[entry.stackName];
    if (!stack) {
      throw new Error(`inventory stack ${entry.stackName} was not constructed`);
    }
    out.push(...collectStackMethods(entry.stackName, stack));
  }
  return out;
}

function compareMethods(a: CollectedMethod, b: CollectedMethod): number {
  if (a.path !== b.path) return a.path < b.path ? -1 : 1;
  if (a.httpMethod !== b.httpMethod) return a.httpMethod < b.httpMethod ? -1 : 1;
  return a.logicalId < b.logicalId ? -1 : a.logicalId > b.logicalId ? 1 : 0;
}

/** Render ROUTE_AUTH.md from the collected methods (deterministic: sorted by stack order, source order, path, verb). */
export function renderRouteAuthMarkdown(
  methods: ReadonlyArray<CollectedMethod>,
  inventory: ReadonlyArray<InventoryStack> = API_STACK_INVENTORY,
  committed: ReadonlyArray<CommittedNoneMethod> = COMMITTED_NONE_METHODS,
): string {
  const controlByKey = new Map(committed.map((c) => [methodKey(c), c.control]));
  const lines: string[] = [];
  const { routes, preflight } = partitionPreflight(methods);
  lines.push('# API route authorization inventory');
  lines.push('');
  lines.push(
    'Generated by `scripts/generate-route-auth.ts` from `lib/route-auth-inventory.ts`; do not edit by hand. ' +
      'Regenerate with `npm run build && npx ts-node scripts/generate-route-auth.ts`; ' +
      '`test/route-auth-inventory.test.ts` compares this file with a fresh rendering.',
  );
  lines.push('');
  lines.push(
    'Universe: synthesized `AWS::ApiGateway::Method` resources of the stacks below and their nested stacks, ' +
      'CORS preflight excluded. A CORS preflight method is an `OPTIONS` method with a `MOCK` integration ' +
      '(emitted by `defaultCorsPreflightOptions`, `NONE` by construction); an `OPTIONS` method with any other ' +
      'integration is listed as a route.',
  );
  lines.push('');
  lines.push(
    `Totals: ${routes.length} routes (` +
      `${routes.filter((m) => m.authorizationType === COGNITO_AUTHORIZATION).length} ${COGNITO_AUTHORIZATION}, ` +
      `${routes.filter((m) => m.authorizationType !== COGNITO_AUTHORIZATION).length} other); ` +
      `${preflight.length} CORS preflight methods excluded.`,
  );
  lines.push('');
  lines.push('## Methods without the Cognito authorizer');
  lines.push('');
  lines.push('| Stack | Method | Path | Control in place of the authorizer |');
  lines.push('|---|---|---|---|');
  for (const c of committed) {
    lines.push(`| ${c.stackName} | ${c.httpMethod} | \`${c.path}\` | ${c.control} |`);
  }
  lines.push('');
  lines.push(
    'Not instantiated by any app entry point or stack (their methods are never deployed): ' +
      UNINSTANTIATED_API_FILES.map((u) => `\`lib/${u.file}\``).join(', ') +
      '.',
  );
  for (const entry of inventory) {
    const stackMethods = methods.filter((m) => m.stackName === entry.stackName);
    const stackSplit = partitionPreflight(stackMethods);
    lines.push('');
    lines.push(`## ${entry.stackName}`);
    lines.push('');
    lines.push(
      entry.registration === 'nested'
        ? 'Methods registered in nested API stacks.'
        : 'Methods registered directly on the imported portal REST API.',
    );
    lines.push('');
    lines.push(`CORS preflight (MOCK, NONE by construction): ${stackSplit.preflight.length}`);
    for (const source of entry.sources) {
      const sourceRoutes = stackSplit.routes
        .filter((m) => m.nestedStackId === source.nestedStackId)
        .sort(compareMethods);
      const sourcePreflight = stackSplit.preflight.filter((m) => m.nestedStackId === source.nestedStackId);
      lines.push('');
      lines.push(
        source.nestedStackId === null
          ? `### Top-level template (\`lib/${source.file}\`)`
          : `### ${source.nestedStackId} (\`lib/${source.file}\`)`,
      );
      lines.push('');
      lines.push(
        `Routes: ${sourceRoutes.length}; CORS preflight (MOCK, NONE by construction): ${sourcePreflight.length}`,
      );
      lines.push('');
      lines.push('| Method | Path | Authorization | Integration | Control |');
      lines.push('|---|---|---|---|---|');
      for (const m of sourceRoutes) {
        const control =
          m.authorizationType === COGNITO_AUTHORIZATION ? '' : controlByKey.get(methodKey(m)) ?? 'NOT COMMITTED';
        lines.push(`| ${m.httpMethod} | \`${m.path}\` | ${m.authorizationType} | ${m.integrationType} | ${control} |`);
      }
    }
    const unlisted = stackMethods.filter(
      (m) => !entry.sources.some((s) => s.nestedStackId === m.nestedStackId),
    );
    if (unlisted.length > 0) {
      lines.push('');
      lines.push(`### Not in the inventory (${unlisted.length})`);
      lines.push('');
      for (const m of [...unlisted].sort(compareMethods)) {
        lines.push(`- ${m.nestedStackId ?? '(top level)'} ${m.httpMethod} \`${m.path}\` ${m.authorizationType}`);
      }
    }
  }
  lines.push('');
  return lines.join('\n');
}
