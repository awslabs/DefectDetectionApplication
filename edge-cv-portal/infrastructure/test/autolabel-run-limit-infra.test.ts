/**
 * Static infrastructure assertions for Images per worker run
 * (autolabel-time-limits-settings, Settings → Auto-Labeling).
 *
 * At the start of every run DdaAutolabelWorker reads the saved Images per
 * worker run and sends the records of its batch beyond that number back to
 * its own queue. The only infrastructure this needs is the queue's URL in the
 * worker's environment and the right to send to that queue; everything else
 * about the worker, the queue and the event source stays as it was:
 *
 * - (a) The worker's AUTOLABEL_QUEUE_URL is the URL of
 *   dda-portal-autolabel-queue.
 * - (b) The worker's role may sqs:SendMessage on that queue.
 * - (c) The event source still hands the worker up to 5 records per run, with
 *   partial batch responses. 5 is MAX_IMAGES_PER_RUN of
 *   backend/functions/autolabel_settings.py, read from that file, so saving
 *   the highest value stays the same as nothing saved.
 * - (d) Unchanged: the worker's 300 s timeout, and the queue's 300 s
 *   visibility timeout and 3 receives before the DLQ.
 * - (e) Exactly two functions carry AUTOLABEL_QUEUE_URL: the Task Distributor
 *   (dda_labeling_worker) and the auto-label worker.
 *
 * Conventions follow grounded-sam-worker-infra.test.ts: the default
 * (no-context) synth once in beforeAll, resources located with
 * template.findResources, assertions on raw CloudFormation properties.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ComputeStack } from '../lib/compute-stack';
import { StorageStack } from '../lib/storage-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const AUTOLABEL_QUEUE_NAME = 'dda-portal-autolabel-queue';
const AUTOLABEL_SETTINGS_PY = path.join(__dirname, '../../backend/functions/autolabel_settings.py');

// Synthesized once: the ComputeStack stages Lambda/layer assets and runs the
// quick-setup bundle packaging script at synth time, which is expensive.
let computeTemplate: Template;

beforeAll(() => {
  // Default synth: no context at all, the shape of a routine portal deploy.
  const app = new cdk.App();

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
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
  });

  computeTemplate = Template.fromStack(compute);
}, 300_000);

/** The single Lambda function in the compute template with `handler`. */
function lambdaByHandler(handler: string): [string, any] {
  const matches = Object.entries(computeTemplate.findResources('AWS::Lambda::Function')).filter(
    ([, resource]) => (resource as any).Properties.Handler === handler
  );
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/** The logical id of the role a function resource executes as. */
function roleRefOf(fn: any): string {
  const roleRef = fn.Properties.Role['Fn::GetAtt']?.[0];
  expect(roleRef).toBeDefined();
  return roleRef;
}

/** The logical id and resource of the auto-label queue. */
function autolabelQueue(): [string, any] {
  const matches = Object.entries(
    computeTemplate.findResources('AWS::SQS::Queue', {
      Properties: { QueueName: AUTOLABEL_QUEUE_NAME },
    })
  );
  expect(matches).toHaveLength(1);
  return matches[0] as [string, any];
}

/**
 * The Allow statements of the IAM policies attached to `roleRef`. The portal
 * Lambda roles' default policies exceed the inline-policy size limit, so CDK
 * splits them into overflow AWS::IAM::ManagedPolicy resources: search both
 * types (the grounded-sam-worker-infra.test.ts precedent).
 */
function allowStatementsOnRole(roleRef: string): any[] {
  const policies = [
    ...Object.values(computeTemplate.findResources('AWS::IAM::Policy')),
    ...Object.values(computeTemplate.findResources('AWS::IAM::ManagedPolicy')),
  ] as any[];
  return policies
    .filter((p) => p.Properties.Roles?.some((r: any) => r.Ref === roleRef))
    .filter((p) => p.Properties.PolicyDocument?.Statement)
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[])
    .filter((s) => s.Effect === 'Allow');
}

function asArray(value: any): any[] {
  return Array.isArray(value) ? value : [value];
}

/** MAX_IMAGES_PER_RUN as autolabel_settings.py defines it. */
function maxImagesPerRun(): number {
  const source = fs.readFileSync(AUTOLABEL_SETTINGS_PY, 'utf8');
  const match = source.match(/^MAX_IMAGES_PER_RUN\s*=\s*(\d+)\b/m);
  expect(match).not.toBeNull();
  return Number(match![1]);
}

describe('DdaAutolabelWorker can send records back to its own queue (Images per worker run)', () => {
  test('(a) AUTOLABEL_QUEUE_URL is the URL of dda-portal-autolabel-queue', () => {
    const [queueId] = autolabelQueue();
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    expect(fn.Properties.Environment.Variables.AUTOLABEL_QUEUE_URL).toEqual({ Ref: queueId });
  });

  test('(b) the worker role is allowed sqs:SendMessage on that queue', () => {
    const [queueId] = autolabelQueue();
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    const queueArn = JSON.stringify({ 'Fn::GetAtt': [queueId, 'Arn'] });
    const sendStatements = allowStatementsOnRole(roleRefOf(fn)).filter(
      (s) =>
        asArray(s.Action).includes('sqs:SendMessage') &&
        asArray(s.Resource).some((resource: any) => JSON.stringify(resource) === queueArn)
    );
    expect(sendStatements.length).toBeGreaterThanOrEqual(1);
  });
});

describe('the worker, its queue and its event source are otherwise unchanged', () => {
  test('(c) the event source hands the worker batches of MAX_IMAGES_PER_RUN (5) with partial batch responses', () => {
    const [queueId] = autolabelQueue();
    const [workerId] = lambdaByHandler('dda_autolabel_worker.handler');
    const mappings = Object.values(computeTemplate.findResources('AWS::Lambda::EventSourceMapping')).filter(
      (mapping: any) => mapping.Properties.FunctionName?.Ref === workerId
    ) as any[];
    expect(mappings).toHaveLength(1);
    const properties = mappings[0].Properties;
    expect(properties.EventSourceArn).toEqual({ 'Fn::GetAtt': [queueId, 'Arn'] });
    expect(properties.BatchSize).toBe(5);
    // Saving the highest Images per worker run must stay the same as
    // nothing saved: the bound and the batch size must not drift apart.
    expect(properties.BatchSize).toBe(maxImagesPerRun());
    expect(properties.FunctionResponseTypes).toEqual(['ReportBatchItemFailures']);
  });

  test('(d) the worker Timeout stays 300; the queue VisibilityTimeout stays 300 and maxReceiveCount 3', () => {
    const [, fn] = lambdaByHandler('dda_autolabel_worker.handler');
    expect(fn.Properties.Timeout).toBe(300);
    const [, queue] = autolabelQueue();
    expect(queue.Properties.VisibilityTimeout).toBe(300);
    expect(queue.Properties.RedrivePolicy.maxReceiveCount).toBe(3);
  });

  test('(e) exactly two functions carry AUTOLABEL_QUEUE_URL: the distributor and the auto-label worker', () => {
    const handlers = Object.values(computeTemplate.findResources('AWS::Lambda::Function'))
      .filter((fn: any) => fn.Properties.Environment?.Variables?.AUTOLABEL_QUEUE_URL !== undefined)
      .map((fn: any) => fn.Properties.Handler)
      .sort();
    expect(handlers).toEqual(['dda_autolabel_worker.handler', 'dda_labeling_worker.handler']);
  });
});
