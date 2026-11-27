/**
 * Static infrastructure assertions for the per-deployment Layout_Check
 * LocalServer floor (car-assembly-check task 12.2; Requirement 9.3; design §7,
 * DD9).
 *
 * - With no `layoutCheckMinLocalServerVersions` context, and with a blank one,
 *   both `workflow_packaging.handler` and `deployments.handler` carry
 *   `WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS = {}` (no floor: fail
 *   closed), and no other Lambda carries the variable.
 * - A configured map, as JSON text or as an object, reaches exactly those two.
 * - A malformed value, an unknown arch or a non-N.N.N version fails the synth
 *   with an error naming the context key.
 *
 * Helpers follow rfdetr-seg-infra.test.ts (synthesize, lambdaWithHandler,
 * variablesOf). Every account id is visibly fake.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const FLOOR_ENV = 'WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS';
const CONTEXT_KEY = 'layoutCheckMinLocalServerVersions';
const FLOOR_HANDLERS = ['workflow_packaging.handler', 'deployments.handler'];

interface Fixture {
  computeTemplate: Template;
  nestedTemplates: Template[];
}

const outdirs: string[] = [];
afterAll(() => {
  for (const dir of outdirs) fs.rmSync(dir, { recursive: true, force: true });
});

/** The real ComputeStack under the given CDK context. */
function synthesize(context: Record<string, unknown>): Fixture {
  const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'layout-check-infra-'));
  outdirs.push(outdir);
  const app = new cdk.App({
    outdir,
    context: {
      ...context,
      'aws:cdk:bundling-stacks': ['Storage', 'Deps', 'Compute', 'Compute/**'],
    },
  });
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const userPool = new cognito.UserPool(deps, 'Pool');
  const compute = new ComputeStack(app, 'Compute', {
    userPool,
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    devicesTable: storage.devicesTable,
    auditLogTable: storage.auditLogTable,
    trainingJobsTable: storage.trainingJobsTable,
    labelingJobsTable: storage.labelingJobsTable,
    labelingTeamsTable: storage.labelingTeamsTable,
    labelingTasksTable: storage.labelingTasksTable,
    preLabeledDatasetsTable: storage.preLabeledDatasetsTable,
    modelsTable: storage.modelsTable,
    deploymentsTable: storage.deploymentsTable,
    settingsTable: storage.settingsTable,
    componentsTable: storage.componentsTable,
    sharedComponentsTable: storage.sharedComponentsTable,
    dataAccountsTable: storage.dataAccountsTable,
    workflowsTable: storage.workflowsTable,
    workflowVersionsTable: storage.workflowVersionsTable,
    testDatasetsTable: storage.testDatasetsTable,
    testRunsTable: storage.testRunsTable,
    workflowChatSessionsTable: storage.workflowChatSessionsTable,
    cameraRegistryTable: storage.cameraRegistryTable,
    deviceRegistrationsTable: storage.deviceRegistrationsTable,
    portalArtifactsBucket: storage.portalArtifactsBucket,
    trustedUseCaseAccountIds: ['111111111111'],
  });
  const computeTemplate = Template.fromStack(compute);
  const nestedTemplates = compute.node
    .findAll()
    .filter((child): child is cdk.NestedStack => child instanceof cdk.NestedStack)
    .map((nested) => Template.fromStack(nested));
  return { computeTemplate, nestedTemplates };
}

function functionsOf(template: Template): any[] {
  return Object.values(template.findResources('AWS::Lambda::Function'));
}

/** The single Lambda function with the given handler in the ComputeStack. */
function lambdaWithHandler(template: Template, handler: string): any {
  const matches = functionsOf(template).filter((r: any) => r.Properties.Handler === handler);
  expect(matches).toHaveLength(1);
  return matches[0];
}

function variablesOf(fn: any): Record<string, any> {
  return fn.Properties.Environment?.Variables ?? {};
}

function carriers(fixture: Fixture): string[] {
  return [fixture.computeTemplate, ...fixture.nestedTemplates]
    .flatMap(functionsOf)
    .filter((fn: any) => variablesOf(fn)[FLOOR_ENV] !== undefined)
    .map((fn: any) => fn.Properties.Handler)
    .sort();
}

const CONFIGURED = { arm64_jp5: '1.0.4' };
let fixtures: Record<string, Fixture>;

beforeAll(() => {
  fixtures = {
    absent: synthesize({}),
    blank: synthesize({ [CONTEXT_KEY]: '' }),
    string: synthesize({ [CONTEXT_KEY]: JSON.stringify(CONFIGURED) }),
    object: synthesize({ [CONTEXT_KEY]: CONFIGURED }),
  };
}, 900_000);

describe('Layout_Check floor environment (Requirement 9.3)', () => {
  const EXPECTED: Record<string, Record<string, string>> = {
    absent: {},
    blank: {},
    string: CONFIGURED,
    object: CONFIGURED,
  };

  test.each(Object.keys(EXPECTED))('%s context: both floor handlers carry the expected map', (name) => {
    for (const handler of FLOOR_HANDLERS) {
      const raw = variablesOf(lambdaWithHandler(fixtures[name].computeTemplate, handler))[FLOOR_ENV];
      expect(typeof raw).toBe('string');
      expect(JSON.parse(raw)).toEqual(EXPECTED[name]);
    }
  });

  test.each(Object.keys(EXPECTED))('%s context: no other Lambda carries the variable', (name) => {
    expect(carriers(fixtures[name])).toEqual([...FLOOR_HANDLERS].sort());
  });

  test('the Seg floor beside it keeps the committed default', () => {
    for (const handler of FLOOR_HANDLERS) {
      const fn = lambdaWithHandler(fixtures.string.computeTemplate, handler);
      expect(JSON.parse(variablesOf(fn).WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS)).toEqual({
        arm64_jp5: '1.0.62',
        arm64_jp6: '1.0.85',
        x86_64: '1.0.56',
        x86_64_nvidia: '1.0.56',
      });
    }
  });

  test.each([['{not json'], ['{"arm64_jp9":"1.0.0"}'], ['{"arm64_jp5":"1.0"}']])(
    'a malformed value %p fails the synth naming the key',
    (value) => {
      expect(() => synthesize({ [CONTEXT_KEY]: value })).toThrow(/layoutCheckMinLocalServerVersions/);
    },
    300_000,
  );
});
