/**
 * Route_Auth_Inventory (portal-authorization-and-iam-scoping, design I3
 * "Route inventory"; task 3.3; Requirements 5.4, 5.5).
 *
 * Synthesizes the six stacks of API_STACK_INVENTORY with the generator's
 * fixture props and asserts over every synthesized AWS::ApiGateway::Method:
 *
 *   (1) every non-preflight method is COGNITO_USER_POOLS except the
 *       committed NONE set;
 *   (2) the collected NONE set equals the committed set;
 *   (3) every method excluded as CORS preflight is OPTIONS with a MOCK
 *       integration;
 *   (4) the committed ROUTE_AUTH.md equals the generator's output;
 *   (5) api-core-stack.ts, api-data-stack.ts and api-model-stack.ts are not
 *       instantiated under bin/ or lib/;
 *   (6) the lib/*.ts files that register methods are exactly the
 *       inventory's files plus those three.
 *
 * fast-check properties on the exported pure predicate and partition: one
 * extra non-preflight NONE method outside the committed set is rejected;
 * the partition excludes exactly the OPTIONS + MOCK methods.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as fc from 'fast-check';
import {
  API_STACK_INVENTORY,
  COGNITO_AUTHORIZATION,
  COMMITTED_NONE_METHODS,
  CollectedMethod,
  UNINSTANTIATED_API_FILES,
  isCorsPreflight,
  methodKey,
  partitionPreflight,
  routeAuthViolations,
} from '../lib/route-auth-inventory';
import {
  InventoryStacks,
  ROUTE_AUTH_PATH,
  buildInventoryStacks,
  generateRouteAuthMarkdown,
} from '../scripts/generate-route-auth';

const INFRA_ROOT = path.join(__dirname, '..');
// The method-registration call, assembled so this file does not match its own search.
const ADD_METHOD_CALL = ['add', 'Method('].join('');

let built: InventoryStacks | undefined;
let methods: CollectedMethod[] = [];
let markdown = '';

beforeAll(() => {
  built = buildInventoryStacks();
  ({ markdown, methods } = generateRouteAuthMarkdown(built));
}, 600_000);

afterAll(() => {
  built?.dispose();
});

/** TypeScript sources (no compiled .js / .d.ts) directly under an infrastructure directory. */
function sourceFiles(dir: string): string[] {
  return fs
    .readdirSync(path.join(INFRA_ROOT, dir))
    .filter((f) => f.endsWith('.ts') && !f.endsWith('.d.ts'))
    .sort();
}

function readSource(dir: string, file: string): string {
  return fs.readFileSync(path.join(INFRA_ROOT, dir, file), 'utf-8');
}

describe('synthesized methods of the inventory stacks', () => {
  test('the collector finds methods in every inventory source and resolves every path', () => {
    expect(methods.length).toBeGreaterThan(0);
    expect(methods.filter((m) => m.path === '<unresolved>')).toEqual([]);
    for (const entry of API_STACK_INVENTORY) {
      for (const source of entry.sources) {
        const routes = partitionPreflight(
          methods.filter((m) => m.stackName === entry.stackName && m.nestedStackId === source.nestedStackId),
        ).routes;
        expect({ source: `${entry.stackName}/${source.nestedStackId}`, routes: routes.length > 0 }).toEqual({
          source: `${entry.stackName}/${source.nestedStackId}`,
          routes: true,
        });
      }
    }
  });

  test('every nested stack holding a method is listed in the inventory', () => {
    const listed = new Set(
      API_STACK_INVENTORY.flatMap((e) => e.sources.map((s) => `${e.stackName}/${s.nestedStackId}`)),
    );
    const holders = [...new Set(methods.map((m) => `${m.stackName}/${m.nestedStackId}`))].sort();
    expect(holders.filter((h) => !listed.has(h))).toEqual([]);
  });

  test('(1) every non-preflight method is COGNITO_USER_POOLS except the committed NONE set', () => {
    expect(routeAuthViolations(methods)).toEqual([]);
    const { routes } = partitionPreflight(methods);
    const committed = new Set(COMMITTED_NONE_METHODS.map(methodKey));
    const offenders = routes
      .filter((m) => m.authorizationType !== COGNITO_AUTHORIZATION && !committed.has(methodKey(m)))
      .map(methodKey);
    expect(offenders).toEqual([]);
  });

  test('(2) the collected NONE set equals the committed set', () => {
    const { routes } = partitionPreflight(methods);
    const collectedNone = routes
      .filter((m) => m.authorizationType === 'NONE')
      .map(methodKey)
      .sort();
    expect(collectedNone).toEqual(COMMITTED_NONE_METHODS.map(methodKey).sort());
    const otherTypes = routes.filter(
      (m) => m.authorizationType !== 'NONE' && m.authorizationType !== COGNITO_AUTHORIZATION,
    );
    expect(otherTypes.map(methodKey)).toEqual([]);
  });

  test('(3) every excluded preflight method is OPTIONS with a MOCK integration and NONE authorization', () => {
    const { preflight } = partitionPreflight(methods);
    expect(preflight.length).toBeGreaterThan(0);
    for (const m of preflight) {
      expect([m.httpMethod, m.integrationType, m.authorizationType]).toEqual(['OPTIONS', 'MOCK', 'NONE']);
    }
    // An OPTIONS method with a real integration would be a route, never excluded.
    const optionsRoutes = methods.filter((m) => m.httpMethod === 'OPTIONS' && m.integrationType !== 'MOCK');
    for (const m of optionsRoutes) {
      expect(partitionPreflight([m]).routes).toEqual([m]);
    }
  });

  test('(4) the committed ROUTE_AUTH.md equals the generator output', () => {
    expect(fs.readFileSync(ROUTE_AUTH_PATH, 'utf-8')).toEqual(markdown);
  });
});

describe('source files', () => {
  test('(5) the three uninstantiated API stack files are not instantiated under bin/ or lib/', () => {
    for (const dir of ['bin', 'lib']) {
      for (const file of sourceFiles(dir)) {
        const text = readSource(dir, file);
        for (const u of UNINSTANTIATED_API_FILES) {
          if (dir === 'lib' && file === u.file) continue;
          expect({ file: `${dir}/${file}`, instantiates: text.includes(`new ${u.className}(`) }).toEqual({
            file: `${dir}/${file}`,
            instantiates: false,
          });
          const moduleName = u.file.replace(/\.ts$/, '');
          expect({ file: `${dir}/${file}`, imports: text.includes(`/${moduleName}'`) }).toEqual({
            file: `${dir}/${file}`,
            imports: false,
          });
        }
      }
    }
  });

  test('(6) the lib files that register methods are the inventory files plus the uninstantiated three', () => {
    const registering = sourceFiles('lib').filter((f) => readSource('lib', f).includes(ADD_METHOD_CALL));
    const expected = [
      ...API_STACK_INVENTORY.flatMap((e) => e.sources.map((s) => s.file)),
      ...UNINSTANTIATED_API_FILES.map((u) => u.file),
    ].sort();
    expect(registering).toEqual(expected);
  });
});

describe('predicate and partition properties', () => {
  const pathArb = fc
    .array(fc.stringMatching(/^[a-z][a-z0-9-]{0,8}$/), { minLength: 1, maxLength: 4 })
    .map((parts) => `/${parts.join('/')}`);
  const stackArb = fc.constantFrom(...API_STACK_INVENTORY.map((e) => e.stackName));
  const verbArb = fc.constantFrom('GET', 'POST', 'PUT', 'DELETE', 'PATCH');
  const method = (
    m: Partial<CollectedMethod> & Pick<CollectedMethod, 'stackName' | 'httpMethod' | 'path'>,
  ): CollectedMethod => ({
    nestedStackId: null,
    logicalId: `M${Math.abs(hash(methodKey(m)))}`,
    authorizationType: COGNITO_AUTHORIZATION,
    integrationType: 'AWS_PROXY',
    ...m,
  });
  function hash(s: string): number {
    let h = 0;
    for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) | 0;
    return h;
  }
  const committedMethods = COMMITTED_NONE_METHODS.map((c) =>
    method({ stackName: c.stackName, httpMethod: c.httpMethod, path: c.path, authorizationType: 'NONE' }),
  );
  const committedKeySet = new Set(COMMITTED_NONE_METHODS.map(methodKey));
  const cognitoRoutesArb = fc.array(
    fc
      .record({ stackName: stackArb, httpMethod: verbArb, path: pathArb })
      .filter((r) => !committedKeySet.has(methodKey(r)))
      .map((r) => method(r)),
    { maxLength: 30 },
  );
  const preflightArb = fc.array(
    fc
      .record({ stackName: stackArb, path: pathArb })
      .map((r) => method({ ...r, httpMethod: 'OPTIONS', authorizationType: 'NONE', integrationType: 'MOCK' })),
    { maxLength: 30 },
  );

  test('a valid method set (committed NONE + Cognito routes + preflight) is accepted', () => {
    fc.assert(
      fc.property(cognitoRoutesArb, preflightArb, (routes, preflight) => {
        expect(routeAuthViolations([...committedMethods, ...routes, ...preflight])).toEqual([]);
      }),
      { numRuns: 200 },
    );
  });

  test('one extra non-preflight NONE method outside the committed set is rejected', () => {
    const committedKeys = committedKeySet;
    const extraArb = fc
      .record({
        stackName: stackArb,
        // Any verb but a preflight one; OPTIONS with a Lambda integration is a route too.
        httpMethod: fc.constantFrom('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'),
        path: pathArb,
        integrationType: fc.constantFrom('AWS_PROXY', 'HTTP_PROXY', 'AWS'),
        authorizationType: fc.constantFrom('NONE', 'AWS_IAM', 'CUSTOM'),
      })
      .filter((m) => !committedKeys.has(methodKey(m)))
      .map((m) => method(m));
    fc.assert(
      fc.property(
        cognitoRoutesArb,
        preflightArb,
        extraArb,
        fc.nat(),
        (routes, preflight, extra, position) => {
          const all = [...committedMethods, ...routes, ...preflight];
          all.splice(position % (all.length + 1), 0, extra);
          const violations = routeAuthViolations(all);
          expect(violations.length).toBeGreaterThan(0);
          expect(violations.some((v) => v.startsWith(methodKey(extra)))).toBe(true);
        },
      ),
      { numRuns: 200 },
    );
  });

  test('a committed NONE method that is missing or carries the authorizer is rejected', () => {
    fc.assert(
      fc.property(fc.nat({ max: committedMethods.length - 1 }), fc.boolean(), (index, drop) => {
        const all = committedMethods.map((m, i) =>
          i === index && !drop ? { ...m, authorizationType: COGNITO_AUTHORIZATION } : m,
        );
        const input = drop ? all.filter((_, i) => i !== index) : all;
        expect(routeAuthViolations(input).length).toBeGreaterThan(0);
      }),
      { numRuns: 50 },
    );
  });

  test('the partition excludes exactly the OPTIONS + MOCK methods, in order', () => {
    const anyMethodArb = fc
      .record({
        stackName: stackArb,
        httpMethod: fc.constantFrom('GET', 'POST', 'OPTIONS'),
        path: pathArb,
        integrationType: fc.constantFrom('MOCK', 'AWS_PROXY', 'HTTP'),
        authorizationType: fc.constantFrom('NONE', COGNITO_AUTHORIZATION),
      })
      .map((m) => method(m));
    fc.assert(
      fc.property(fc.array(anyMethodArb, { maxLength: 40 }), (list) => {
        const { routes, preflight } = partitionPreflight(list);
        expect(preflight).toEqual(list.filter((m) => m.httpMethod === 'OPTIONS' && m.integrationType === 'MOCK'));
        expect(routes).toEqual(list.filter((m) => !(m.httpMethod === 'OPTIONS' && m.integrationType === 'MOCK')));
        expect(preflight.every(isCorsPreflight)).toBe(true);
        expect(routes.some(isCorsPreflight)).toBe(false);
      }),
      { numRuns: 200 },
    );
  });
});
