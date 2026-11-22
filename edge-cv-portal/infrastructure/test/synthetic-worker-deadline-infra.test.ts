/**
 * Static infrastructure assertions for fix-synthetic-worker-deadline (the
 * synthetic generation worker against the Lambda timeout).
 *
 * A 50-variation pass (about 25 s per variation) outlives the worker's
 * 15 min timeout; under Lambda's default asynchronous retry policy (2
 * retries, applied because the function had no EventInvokeConfig) a
 * timed-out worker re-ran the whole plan twice: 130 preview records for 50
 * tasks on session 6115b94c. The worker now hands the unfinished tail of
 * the plan to a continuation invocation itself and reports a dead pass
 * through the session, so:
 *
 * 1. the handler carries exactly one AWS::Lambda::EventInvokeConfig with
 *    MaximumRetryAttempts 0 and a 2 min MaximumEventAgeInSeconds (the
 *    API's staleness grace covers a continuation that starts that late);
 * 2. the handler reads its own time limit: SYNTHETIC_WORKER_TIMEOUT_SECONDS
 *    equals the function's Timeout (900), both from one Duration.
 *
 * Conventions follow synthetic-data-s3-permissions.test.ts: synthesize once
 * in beforeAll with a generous timeout, locate resources via
 * template.findResources, assert on raw CloudFormation properties.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { StorageStack } from '../lib/storage-stack';
import { SyntheticDataStack } from '../lib/synthetic-data-stack';

const TRUSTED_USECASE_ACCOUNT = '111111111111';
const HANDLER_FUNCTION_NAME = 'dda-synthetic-data-handler';

let syntheticTemplate: Template;

beforeAll(() => {
  const app = new cdk.App();
  const storage = new StorageStack(app, 'Storage');
  const deps = new cdk.Stack(app, 'Deps');
  const synthetic = new SyntheticDataStack(app, 'SyntheticData', {
    useCasesTable: storage.useCasesTable,
    userRolesTable: storage.userRolesTable,
    auditLogTable: storage.auditLogTable,
    settingsTable: storage.settingsTable,
    trainingJobsTable: storage.trainingJobsTable,
    // The stack throws at synth time on an empty list — always pass one.
    trustedUseCaseAccountIds: [TRUSTED_USECASE_ACCOUNT],
    userPool: new cognito.UserPool(deps, 'Pool'),
    restApiId: 'testrestapiid',
    restApiRootResourceId: 'testrootresourceid',
    apiStageName: 'prod',
  });
  syntheticTemplate = Template.fromStack(synthetic);
}, 300_000);

function handlerEntry(): [string, any] {
  const functions = syntheticTemplate.findResources('AWS::Lambda::Function');
  const entries = Object.entries(functions).filter(
    ([, resource]) =>
      resource.Properties.FunctionName === HANDLER_FUNCTION_NAME
  );
  expect(entries).toHaveLength(1);
  expect(Object.keys(functions)).toHaveLength(1);
  expect(entries[0][0].startsWith('SyntheticDataHandler')).toBe(true);
  return entries[0] as [string, any];
}

describe('synthetic data worker: no asynchronous retries', () => {
  test('exactly one EventInvokeConfig, on the handler, retries off, 2 min event age', () => {
    const [handlerLogicalId] = handlerEntry();
    const configs = syntheticTemplate.findResources(
      'AWS::Lambda::EventInvokeConfig'
    );
    const entries = Object.values(configs);
    expect(entries).toHaveLength(1);
    const properties = entries[0].Properties;
    expect(properties.FunctionName).toEqual({ Ref: handlerLogicalId });
    expect(properties.Qualifier).toBe('$LATEST');
    expect(properties.MaximumRetryAttempts).toBe(0);
    expect(properties.MaximumEventAgeInSeconds).toBe(120);
    // No destination: the session META is the durable record of a pass.
    expect(properties.DestinationConfig).toBeUndefined();
  });
});

describe('synthetic data worker: the handler reads its own time limit', () => {
  test('SYNTHETIC_WORKER_TIMEOUT_SECONDS equals the 900 s function Timeout', () => {
    const [, handler] = handlerEntry();
    expect(handler.Properties.Timeout).toBe(900);
    expect(
      handler.Properties.Environment.Variables.SYNTHETIC_WORKER_TIMEOUT_SECONDS
    ).toBe(String(handler.Properties.Timeout));
  });
});
