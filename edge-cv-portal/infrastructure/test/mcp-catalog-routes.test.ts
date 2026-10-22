/**
 * The jest half of the catalog-route registration test (mcp-servers design
 * Testing Strategy row "Catalog routes are registered Portal_API methods",
 * review-3 M1; tasks.md 8.4). Requirements 1.3, 16.6, 3.1.
 *
 * Every `(method, resource)` of `mcp_portal/catalog_routes.json` (pinned to
 * the Python catalog by `tests/mcp/test_mcp_catalog_routes_export.py`) must
 * be a method registered on the portal REST API. The registered set is read
 * from the synthesized templates of the ComputeStack and its nested API
 * stacks (by construct id), NodeDesignerStack's NodeDesignerApi,
 * BuildFleetStack and SyntheticDataStack, built by the route-auth inventory
 * fixture. Each Method's path is walked up its `ParentId` chain inside its
 * own template; a parent imported from another stack is named by the prefix
 * table below, and an imported parent absent from the table fails the test.
 * The account-lifecycle routes of Backlog 2 are absent from both sides.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { collectStackMethods } from '../lib/route-auth-inventory';
import { InventoryStacks, buildInventoryStacks } from '../scripts/generate-route-auth';

const CATALOG_ROUTES = path.join(
  __dirname, '../../backend/functions/mcp_portal/catalog_routes.json',
);

/** Imported parent construct id -> the path of that resource on the portal API. */
const IMPORTED_PARENT_PREFIX: Record<string, string> = {
  PortalApi: '',
  DeviceResource: '/devices/{id}',
  LabelingJobResource: '/labeling/{id}',
  WorkflowGenerateResource: '/workflows/generate',
  WorkflowResource: '/workflows/{id}',
};

/** The holders whose templates register the catalog's routes. */
const HOLDERS: ReadonlyArray<{ stackName: string; nestedIds: ReadonlyArray<string | null> }> = [
  {
    stackName: 'EdgeCVPortalComputeStack',
    nestedIds: ['ApiGateway', 'CameraRegistryApi', 'UserAdminApi', 'QuickSetupApi',
      'DdaLabelingApi', 'WorkflowManagerGapsApi', 'WorkflowTuningApi'],
  },
  { stackName: 'EdgeCVPortalNodeDesignerStack', nestedIds: ['NodeDesignerApi'] },
  { stackName: 'EdgeCVPortalBuildFleetStack', nestedIds: [null] },
  { stackName: 'EdgeCVPortalSyntheticDataStack', nestedIds: [null] },
];

const ACCOUNT_LIFECYCLE_ROUTES = [
  'POST /admin/users',
  'POST /admin/users/{username}/disable',
  'POST /admin/users/{username}/enable',
  'DELETE /admin/users/{username}',
];

interface CatalogRoute {
  method: string;
  resource: string;
  backend: string;
}

/** Resolved resource id of each imported parent of one holder -> its path. */
function importedParents(holder: cdk.Stack): Map<string, string> {
  const parents = new Map<string, string>();
  for (const construct of holder.node.findAll()) {
    const prefix = IMPORTED_PARENT_PREFIX[construct.node.id];
    if (prefix === undefined || cdk.Stack.of(construct) !== holder) continue;
    const imported = construct as any;
    const resourceId =
      'restApiRootResourceId' in imported ? imported.restApiRootResourceId : imported.resourceId;
    if (resourceId === undefined) continue;
    parents.set(JSON.stringify(holder.resolve(resourceId)), prefix);
  }
  return parents;
}

/** `METHOD /path` of every non-OPTIONS method in one holder's template. */
function walkRoutes(holder: cdk.Stack): string[] {
  const resources = Template.fromStack(holder).toJSON().Resources as Record<string, any>;
  const imported = importedParents(holder);
  const parentPath = (ref: any): string => {
    if (ref?.Ref && resources[ref.Ref]?.Type === 'AWS::ApiGateway::Resource') {
      return pathOf(ref.Ref);
    }
    const getAtt = ref?.['Fn::GetAtt'];
    if (getAtt && getAtt[1] === 'RootResourceId'
        && resources[getAtt[0]]?.Type === 'AWS::ApiGateway::RestApi') {
      return '';
    }
    const key = JSON.stringify(ref);
    const prefix = imported.get(key);
    if (prefix === undefined) {
      throw new Error(`${holder.node.path}: imported parent ${key} is not in the prefix table`);
    }
    return prefix;
  };
  const pathOf = (logicalId: string): string => {
    const props = resources[logicalId].Properties;
    return `${parentPath(props.ParentId)}/${props.PathPart}`;
  };
  return Object.values(resources)
    .filter((r) => r.Type === 'AWS::ApiGateway::Method' && r.Properties.HttpMethod !== 'OPTIONS')
    .map((r) => `${r.Properties.HttpMethod} ${parentPath(r.Properties.ResourceId) || '/'}`);
}

function holderOf(stack: cdk.Stack, nestedId: string | null): cdk.Stack {
  if (nestedId === null) return stack;
  const nested = stack.node.tryFindChild(nestedId);
  if (!nested || !cdk.NestedStack.isNestedStack(nested)) {
    throw new Error(`${stack.node.id} has no nested stack ${nestedId}`);
  }
  return nested;
}

let built: InventoryStacks | undefined;
let registered = new Set<string>();
let collected = new Set<string>();
let catalog: CatalogRoute[] = [];

beforeAll(() => {
  built = buildInventoryStacks();
  for (const { stackName, nestedIds } of HOLDERS) {
    const stack = built.stacks[stackName];
    for (const nestedId of nestedIds) {
      walkRoutes(holderOf(stack, nestedId)).forEach((route) => registered.add(route));
    }
    for (const method of collectStackMethods(stackName, stack)) {
      if (method.httpMethod !== 'OPTIONS') collected.add(`${method.httpMethod} ${method.path}`);
    }
  }
  catalog = JSON.parse(fs.readFileSync(CATALOG_ROUTES, 'utf8'));
}, 900_000);

afterAll(() => {
  built?.dispose();
});

describe('catalog routes are registered Portal_API methods', () => {
  test('the walker reads every holder and agrees with the construct-level paths', () => {
    expect(registered.size).toBeGreaterThan(100);
    expect([...registered].sort()).toEqual([...collected].sort());
  });

  test('every catalog (method, resource) is a registered method, parameter names included', () => {
    expect(catalog.length).toBeGreaterThan(0);
    const missing = catalog
      .map((route) => `${route.method} ${route.resource}`)
      .filter((route) => !registered.has(route));
    expect(missing).toEqual([]);
  });

  test('the account-lifecycle routes are absent from the API and from the catalog', () => {
    const inCatalog = new Set(catalog.map((route) => `${route.method} ${route.resource}`));
    for (const route of ACCOUNT_LIFECYCLE_ROUTES) {
      expect(registered.has(route)).toBe(false);
      expect(inCatalog.has(route)).toBe(false);
    }
  });
});
