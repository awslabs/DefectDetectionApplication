/**
 * Static infrastructure assertions for RF-DETR-Seg instance segmentation
 * (rfdetr-seg-support tasks 14.1–14.6; Requirements 14.1, 14.2, 14.3, 14.7,
 * 14.8, 15.1, 15.3, 17.6).
 *
 * - 15.1 / 15.3: the Seg_Switch — `PORTAL_RFDETR_SEG_ENABLED` is 'true' with
 *   no context and 'false' with `-c enableRfDetrSeg=false` on exactly the
 *   training, model converter and packaging handlers; absent from the
 *   deployments and workflow packaging handlers, from the MCP Lambda (the MCP
 *   tools relay the handlers' 403, they never read the switch) and from the
 *   shared lambdaEnvironment; the `RfDetrSegEnabled` output carries the same
 *   value and no Export.
 * - 14.8 (a), (e): both `workflow_packaging.handler` and `deployments.handler`
 *   carry `WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS`, the parsed value
 *   equals the literal (`{}` pre-enablement), and no other Lambda carries it
 *   (the camera-registry-stream-credentials-infra.test.ts pattern).
 * - 14.1: the TrainingHandler bundle carries `detection_training/
 *   train_rfdetr_seg.py` beside `train_rfdetr.py`.
 * - 14.2: the layer tick list — every handler the Seg paths touch has the
 *   layer its imports need (design §14 "Layers per handler").
 * - 14.3: `captures.handler` at 1024 MB with the shared environment key set,
 *   and the ComputeStack template under the 1 MB / 500-resource limits.
 *
 * Every account id is visibly fake.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { ComputeStack } from '../lib/compute-stack';
import { rfDetrSegEnabled } from '../lib/context-helpers';
import { McpStack, mcpBackendHandlers } from '../lib/mcp-stack';
import { StorageStack } from '../lib/storage-stack';

const SWITCH_ENV = 'PORTAL_RFDETR_SEG_ENABLED';
const FLOOR_ENV = 'WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS';
const SWITCH_HANDLERS = ['training.handler', 'model_converter.handler', 'packaging.handler'];
const FLOOR_HANDLERS = ['workflow_packaging.handler', 'deployments.handler'];
const MCP_HANDLER = 'mcp_portal.handler.handler';
const INFRA_DIR = path.join(__dirname, '..');

interface Fixture {
  app: cdk.App;
  compute: ComputeStack;
  computeTemplate: Template;
  /** The ComputeStack's nested stacks (the API stacks), synthesized. */
  nestedTemplates: Template[];
  mcpTemplate?: Template;
  outdir: string;
}

const outdirs: string[] = [];
afterAll(() => {
  for (const dir of outdirs) fs.rmSync(dir, { recursive: true, force: true });
});

/** The real ComputeStack under the given CDK context, plus (optionally) the
 *  McpStack consuming its handlers — the mcp-stack.test.ts fixture, whose
 *  bundling-stacks context keeps the McpLayer's pip bundling out of the run. */
function synthesize(context: Record<string, unknown>, withMcp: boolean): Fixture {
  const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'rfdetr-seg-infra-'));
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
  let mcpTemplate: Template | undefined;
  if (withMcp) {
    const userPoolDomain = userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: 'fake-portal-domain' },
    });
    const fn = (id: string) =>
      new lambda.Function(deps, id, {
        runtime: lambda.Runtime.PYTHON_3_11,
        handler: 'index.handler',
        code: lambda.Code.fromInline('def handler(event, context):\n    return {}\n'),
      });
    const mcp = new McpStack(app, 'EdgeCVPortalMcpStack', {
      userPool,
      userPoolDomain,
      userRolesTable: storage.userRolesTable,
      auditLogTable: storage.auditLogTable,
      usecasesTable: storage.useCasesTable,
      backendHandlers: mcpBackendHandlers({
        computeHandlers: compute.mcpBackendHandlers,
        nodeDesigner: {
          pluginRecordsHandler: fn('PluginRecords'),
          pluginImporterHandler: fn('PluginImporter'),
          nodeGeneratorHandler: fn('NodeGenerator'),
          pluginBuildsHandler: fn('PluginBuilds'),
          pluginComponentsHandler: fn('PluginComponents'),
          pluginSimulatorHandler: fn('PluginSimulator'),
          customNodeTypesHandler: fn('CustomNodeTypes'),
        },
        buildFleet: {
          buildJobsHandler: fn('BuildJobs'),
          buildFleetHandler: fn('BuildFleet'),
          buildConfigHandler: fn('BuildConfig'),
        },
        syntheticData: { syntheticDataHandler: fn('SyntheticData') },
      }),
      mcpMutatingToolsEnabled: 'false',
      mcpRedirectUris: [],
      mcpBuildSha: 'fake0000sha0',
    });
    mcpTemplate = Template.fromStack(mcp);
  }
  const computeTemplate = Template.fromStack(compute);
  const nestedTemplates = compute.node
    .findAll()
    .filter((child): child is cdk.NestedStack => child instanceof cdk.NestedStack)
    .map((nested) => Template.fromStack(nested));
  return { app, compute, computeTemplate, nestedTemplates, mcpTemplate, outdir };
}

let onByDefault: Fixture;
let switchedOff: Fixture;

beforeAll(() => {
  onByDefault = synthesize({}, true);
  switchedOff = synthesize({ enableRfDetrSeg: 'false' }, false);
}, 900_000);

const asList = (value: any): any[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

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

/** The layer logical-id prefixes a function references, in order. */
function layerPrefixesOf(fn: any): string[] {
  return asList(fn.Properties.Layers).map((layer: any) =>
    String(layer.Ref).replace(/[0-9A-F]{8}$/, ''),
  );
}

// ---------------------------------------------------------------------------
// context-helpers: the Seg_Switch rule (Requirement 15.1 criterion 1)
// ---------------------------------------------------------------------------
describe('rfDetrSegEnabled (context-helpers)', () => {
  test('only an explicit false disables', () => {
    expect(rfDetrSegEnabled(false)).toBe(false);
    expect(rfDetrSegEnabled('false')).toBe(false);
    expect(rfDetrSegEnabled(' FALSE ')).toBe(false);
  });
  test('absent, true and every other value leave the feature on', () => {
    for (const value of [undefined, null, true, 'true', '', '0', 'no', 'off', 'flase', 0]) {
      expect(rfDetrSegEnabled(value)).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// The Seg_Switch in the ComputeStack (Requirements 15.1, 15.3)
// ---------------------------------------------------------------------------
describe('Seg_Switch environment (Requirement 15.1)', () => {
  test.each(SWITCH_HANDLERS)('%s carries PORTAL_RFDETR_SEG_ENABLED=true with no context', (handler) => {
    expect(variablesOf(lambdaWithHandler(onByDefault.computeTemplate, handler))[SWITCH_ENV]).toBe('true');
  });

  test.each(SWITCH_HANDLERS)('%s carries PORTAL_RFDETR_SEG_ENABLED=false with enableRfDetrSeg=false', (handler) => {
    expect(variablesOf(lambdaWithHandler(switchedOff.computeTemplate, handler))[SWITCH_ENV]).toBe('false');
  });

  test.each([...FLOOR_HANDLERS, 'captures.handler', 'compilation.handler'])(
    '%s does not carry the switch (never the shared lambdaEnvironment)',
    (handler) => {
      for (const fixture of [onByDefault, switchedOff]) {
        expect(variablesOf(lambdaWithHandler(fixture.computeTemplate, handler))).not.toHaveProperty(SWITCH_ENV);
      }
    },
  );

  test('exactly the three handlers carry the switch, in the ComputeStack and its nested stacks', () => {
    for (const fixture of [onByDefault, switchedOff]) {
      const carriers = [fixture.computeTemplate, ...fixture.nestedTemplates]
        .flatMap(functionsOf)
        .filter((fn: any) => variablesOf(fn)[SWITCH_ENV] !== undefined)
        .map((fn: any) => fn.Properties.Handler)
        .sort();
      expect(carriers).toEqual([...SWITCH_HANDLERS].sort());
    }
  });

  test('the MCP Lambda does not read the switch (Requirement 15.2)', () => {
    const mcp = lambdaWithHandler(onByDefault.mcpTemplate!, MCP_HANDLER);
    expect(variablesOf(mcp)).not.toHaveProperty(SWITCH_ENV);
    expect(variablesOf(mcp)).not.toHaveProperty(FLOOR_ENV);
  });

  test('the RfDetrSegEnabled output carries the deployed value and no Export', () => {
    const on = onByDefault.computeTemplate.toJSON().Outputs.RfDetrSegEnabled;
    expect(on.Value).toBe('true');
    expect(on).not.toHaveProperty('Export');
    const off = switchedOff.computeTemplate.toJSON().Outputs.RfDetrSegEnabled;
    expect(off.Value).toBe('false');
    expect(off).not.toHaveProperty('Export');
  });
});

// ---------------------------------------------------------------------------
// The Seg feature floor literal (Requirement 14.8 (a), (e); design DD8)
// ---------------------------------------------------------------------------
describe('RF-DETR-Seg feature floor (Requirement 14.8)', () => {
  // Filled by spec group 20 (task 20.1) with the first Seg-capable LocalServer
  // build of each architecture, arm64_jp6 then raised to the rebuilt image a
  // JP6 device verified (group 22). arm64_cpu (no Portal build target) and
  // arm64_jp7 (no device verified its build, Requirement 14.8 d iii) are
  // listed as unverified in the backend coverage test, which requires every
  // other packager arch here. This expectation moves with the literal.
  const DEPLOYED_FLOOR: Record<string, string> = {
    arm64_jp5: '1.0.62',
    arm64_jp6: '1.0.85',
    x86_64: '1.0.56',
    x86_64_nvidia: '1.0.56',
  };

  test.each(FLOOR_HANDLERS)('%s reads the Seg feature floor literal', (handler) => {
    const fn = lambdaWithHandler(onByDefault.computeTemplate, handler);
    const raw = variablesOf(fn)[FLOOR_ENV];
    expect(typeof raw).toBe('string');
    expect(JSON.parse(raw)).toEqual(DEPLOYED_FLOOR);
    // The stream floor stays beside it, untouched (14.8 f).
    expect(variablesOf(fn)).toHaveProperty('WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS');
  });

  test('no other Lambda carries the Seg floor literal', () => {
    const carriers = [onByDefault.computeTemplate, ...onByDefault.nestedTemplates]
      .flatMap(functionsOf)
      .filter((fn: any) => variablesOf(fn)[FLOOR_ENV] !== undefined)
      .map((fn: any) => fn.Properties.Handler)
      .sort();
    expect(carriers).toEqual([...FLOOR_HANDLERS].sort());
  });

  test('the switch does not move the floor literal', () => {
    for (const handler of FLOOR_HANDLERS) {
      expect(JSON.parse(variablesOf(lambdaWithHandler(switchedOff.computeTemplate, handler))[FLOOR_ENV])).toEqual(
        DEPLOYED_FLOOR,
      );
    }
  });

  test('the lineage floor literal is not raised for Seg (14.8 f)', () => {
    const fn = lambdaWithHandler(onByDefault.computeTemplate, 'workflow_packaging.handler');
    expect(JSON.parse(variablesOf(fn).WORKFLOW_MIN_LOCAL_SERVER_VERSIONS)).toEqual({
      arm64_cpu: '1.1.0',
      arm64_jp5: '1.0.0',
      arm64_jp6: '1.0.0',
      arm64_jp7: '1.0.0',
      x86_64: '1.0.0',
      x86_64_nvidia: '1.0.0',
    });
  });

  // The per-deployment key (car-assembly-check task 12.5; design §7, DD9):
  // `rfdetrSegMinLocalServerVersions` replaces the literal above on a
  // deployment that sets it; absent or blank keeps the literal.
  describe('rfdetrSegMinLocalServerVersions (car-assembly-check 12.5)', () => {
    const CONTEXT_KEY = 'rfdetrSegMinLocalServerVersions';
    const CONFIGURED = { arm64_jp5: '1.0.4' };
    const EXPECTED: Record<string, Record<string, string>> = {
      blank: DEPLOYED_FLOOR,
      emptyMap: {},
      string: CONFIGURED,
      object: CONFIGURED,
    };
    let configured: Record<string, Fixture>;
    beforeAll(() => {
      configured = {
        blank: synthesize({ [CONTEXT_KEY]: '' }, false),
        emptyMap: synthesize({ [CONTEXT_KEY]: '{}' }, false),
        string: synthesize({ [CONTEXT_KEY]: JSON.stringify(CONFIGURED) }, false),
        object: synthesize({ [CONTEXT_KEY]: CONFIGURED }, false),
      };
    }, 900_000);

    test.each(Object.keys(EXPECTED))('%s context: exactly the two floor handlers carry the expected map', (name) => {
      for (const handler of FLOOR_HANDLERS) {
        const raw = variablesOf(lambdaWithHandler(configured[name].computeTemplate, handler))[FLOOR_ENV];
        expect(JSON.parse(raw)).toEqual(EXPECTED[name]);
      }
      const carriers = [configured[name].computeTemplate, ...configured[name].nestedTemplates]
        .flatMap(functionsOf)
        .filter((fn: any) => variablesOf(fn)[FLOOR_ENV] !== undefined)
        .map((fn: any) => fn.Properties.Handler)
        .sort();
      expect(carriers).toEqual([...FLOOR_HANDLERS].sort());
    });

    test.each([['{not json'], ['{"arm64_jp9":"1.0.0"}'], ['{"arm64_jp5":"1.0"}']])(
      'a malformed value %p fails the synth naming the key',
      (value) => {
        expect(() => synthesize({ [CONTEXT_KEY]: value }, false)).toThrow(/rfdetrSegMinLocalServerVersions/);
      },
      300_000,
    );
  });
});

// ---------------------------------------------------------------------------
// The TrainingHandler bundle (Requirement 14.1)
// ---------------------------------------------------------------------------
describe('TrainingHandler bundle (Requirement 14.1)', () => {
  test('detectionTrainingFiles lists the Seg entry point beside train_rfdetr.py', () => {
    const source = fs.readFileSync(path.join(INFRA_DIR, 'lib', 'compute-stack.ts'), 'utf8');
    const list = source.match(/const detectionTrainingFiles = \[([\s\S]*?)\];/);
    expect(list).not.toBeNull();
    const entries = [...list![1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
    expect(entries).toContain('detection_training/train_rfdetr.py');
    expect(entries).toContain('detection_training/train_rfdetr_seg.py');
    expect(entries).toContain('detection_training/_common.py');
    expect(entries).toContain('detection_training/requirements-rfdetr.txt');
  });

  test('the synthesized asset carries detection_training/train_rfdetr_seg.py', () => {
    const fn = lambdaWithHandler(onByDefault.computeTemplate, 'training.handler');
    const key: string = fn.Properties.Code.S3Key;
    expect(typeof key).toBe('string');
    const assetDir = path.join(onByDefault.outdir, `asset.${key.replace(/\.zip$/, '')}`);
    for (const rel of ['train_rfdetr.py', 'train_rfdetr_seg.py', '_common.py', 'requirements-rfdetr.txt']) {
      expect(fs.existsSync(path.join(assetDir, 'detection_training', rel))).toBe(true);
    }
    // The bundle is the one source of truth for what the job can tar: the
    // Seg entry point's bundled copy is byte-identical to datasets/.
    const bundled = fs.readFileSync(path.join(assetDir, 'detection_training', 'train_rfdetr_seg.py'));
    const source = fs.readFileSync(
      path.join(INFRA_DIR, '..', '..', 'datasets', 'detection_training', 'train_rfdetr_seg.py'),
    );
    expect(bundled.equals(source)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Layers per handler (Requirement 14.2; design §14 "Layers per handler")
// ---------------------------------------------------------------------------
describe('layers per handler (Requirement 14.2)', () => {
  test.each([
    ['training.handler', ['SharedLayer']],
    ['packaging.handler', ['SharedLayer']],
    ['model_converter.handler', ['SharedLayer']],
    ['model_import.handler', ['SharedLayer']],
    ['captures.handler', ['SharedLayer']],
    ['training_events.handler', ['SharedLayer']],
    ['dda_labeling.handler', ['SharedLayer', 'ImagingLayer']],
    ['dda_labeling_worker.handler', ['SharedLayer', 'ImagingLayer']],
    ['dda_autolabel_worker.handler', ['SharedLayer', 'ImagingLayer']],
    ['deployments.handler', ['SharedLayer', 'WorkflowCoreLayer']],
    ['workflow_packaging.handler', ['SharedLayer', 'WorkflowCoreLayer']],
  ])('%s carries exactly %j', (handler, layers) => {
    expect(layerPrefixesOf(lambdaWithHandler(onByDefault.computeTemplate, handler))).toEqual(layers);
  });
});

// ---------------------------------------------------------------------------
// CapturesHandler and the template limits (Requirements 11.1, 14.3, 14.7)
// ---------------------------------------------------------------------------
describe('CapturesHandler memory and the ComputeStack limits (Requirement 14.3)', () => {
  test('captures.handler runs at 1024 MB with the shared environment key set', () => {
    onByDefault.computeTemplate.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'captures.handler',
      MemorySize: 1024,
    });
    // `environment: lambdaEnvironment`, like the compilation handler: no
    // handler-specific key joined it.
    const captures = variablesOf(lambdaWithHandler(onByDefault.computeTemplate, 'captures.handler'));
    const shared = variablesOf(lambdaWithHandler(onByDefault.computeTemplate, 'compilation.handler'));
    expect(Object.keys(captures).sort()).toEqual(Object.keys(shared).sort());
    expect(captures).not.toHaveProperty(SWITCH_ENV);
    expect(captures).not.toHaveProperty(FLOOR_ENV);
  });

  test('the ComputeStack template stays under 1,000,000 bytes and 500 resources (recorded for the verification notes)', () => {
    const template = onByDefault.computeTemplate.toJSON();
    const bytes = Buffer.byteLength(JSON.stringify(template), 'utf8');
    const resources = Object.keys(template.Resources).length;
    // eslint-disable-next-line no-console
    console.log(`ComputeStack template: ${bytes} bytes, ${resources} resources`);
    expect(bytes).toBeLessThan(1_000_000);
    expect(resources).toBeLessThan(500);
  });
});
