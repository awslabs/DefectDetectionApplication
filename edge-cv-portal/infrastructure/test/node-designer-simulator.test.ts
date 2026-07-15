/**
 * Static infrastructure assertions for the Plugin_Simulator sandbox
 * (custom-node-designer task 8.5).
 *
 * Covers the infrastructure half of the simulator integration suite (the
 * runtime half lives in backend/tests/test_plugin_simulator.py and the
 * containerized tests in test-sandbox/tests/integration/test_simulate_e2e.py):
 *
 * - Task-role policy assertions (Requirement 7.2): every S3 grant on the
 *   simulator Fargate task role covers exactly the plugin-simulations/ run
 *   prefix. No Plugin_Library (workflow-plugins/) write path, no
 *   plugin-sources/ or plugin-staging/ prefixes, no bucket-wide listing, and
 *   no non-S3 data-plane access (DynamoDB, Greengrass, IoT, KMS, ...).
 * - The sandbox task definition uses that task role and dispatches the image
 *   to the single-plugin harness with HARNESS_MODE=simulate.
 * - The state machine's RunSandbox state carries the 5-minute task timeout
 *   that stops the Fargate run and routes to the timeout recorder retaining
 *   flushed partial results (Requirement 7.7).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { NodeDesignerStack } from '../lib/node-designer-stack';

// Synthesized once: asset staging for the Lambda code/layers is expensive.
let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const deps = new cdk.Stack(app, 'Deps');
  const table = (id: string) =>
    new dynamodb.Table(deps, id, {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    });

  const stack = new NodeDesignerStack(app, 'NodeDesigner', {
    portalArtifactsBucket: new s3.Bucket(deps, 'Artifacts'),
    useCasesTable: table('UseCases'),
    userRolesTable: table('UserRoles'),
    auditLogTable: table('AuditLog'),
    settingsTable: table('Settings'),
    workflowsTable: table('Workflows'),
    workflowVersionsTable: table('WorkflowVersions'),
    testDatasetsTable: table('TestDatasets'),
    trustedUseCaseAccountIds: ['111111111111'],
    userPool: new cognito.UserPool(deps, 'Pool'),
    restApiId: 'testrestapi',
    restApiRootResourceId: 'testrootresource',
    apiStageName: 'v1',
  });

  template = Template.fromStack(stack);
}, 120_000);

/** Flattens a statement's Action / Resource entries to arrays. */
const asArray = (value: any): any[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

/** Serialized form of a resource entry (resolves Fn::Join token structure). */
const resourceText = (resource: any): string =>
  typeof resource === 'string' ? resource : JSON.stringify(resource);

/** Logical id of the simulator sandbox task role. */
function taskRoleLogicalId(): string {
  const roles = template.findResources('AWS::IAM::Role');
  const ids = Object.keys(roles).filter((id) =>
    id.startsWith('SimulatorSandboxTaskRole')
  );
  expect(ids).toHaveLength(1);
  return ids[0];
}

/**
 * Every IAM policy statement that applies to the simulator task role:
 * inline policies attached via the role's Policies property plus every
 * AWS::IAM::Policy resource whose Roles list references the role. This is
 * the complete data-plane surface of the sandbox task (7.2).
 */
function taskRoleStatements(): any[] {
  const roleId = taskRoleLogicalId();
  const statements: any[] = [];

  const role = template.findResources('AWS::IAM::Role')[roleId];
  for (const inline of asArray((role as any).Properties.Policies)) {
    statements.push(...inline.PolicyDocument.Statement);
  }

  for (const policy of Object.values(
    template.findResources('AWS::IAM::Policy')
  )) {
    const attachedRoles = asArray((policy as any).Properties.Roles);
    const attached = attachedRoles.some(
      (ref: any) => JSON.stringify(ref).includes(roleId)
    );
    if (attached) {
      statements.push(...(policy as any).Properties.PolicyDocument.Statement);
    }
  }

  expect(statements.length).toBeGreaterThan(0);
  return statements;
}

/** The simulator sandbox ECS task definition. */
function simulatorTaskDefinition(): any {
  const definitions = template.findResources('AWS::ECS::TaskDefinition');
  const entries = Object.entries(definitions).filter(([id]) =>
    id.startsWith('SimulatorTaskDefinition')
  );
  expect(entries).toHaveLength(1);
  return entries[0][1];
}

describe('simulator sandbox task-role policy (Requirement 7.2)', () => {
  test('the task role is assumed by ECS tasks only', () => {
    const role: any =
      template.findResources('AWS::IAM::Role')[taskRoleLogicalId()];
    const principals = role.Properties.AssumeRolePolicyDocument.Statement.map(
      (s: any) => s.Principal?.Service
    );
    expect(principals).toEqual(['ecs-tasks.amazonaws.com']);
    // No managed policies smuggling in broader access.
    expect(role.Properties.ManagedPolicyArns).toBeUndefined();
  });

  test('every S3 object grant covers only the plugin-simulations/ run prefix', () => {
    for (const statement of taskRoleStatements()) {
      const objectActions = asArray(statement.Action).filter(
        (a: string) => a.startsWith('s3:') && !/^s3:List/.test(a)
      );
      if (objectActions.length === 0) {
        continue;
      }
      for (const resource of asArray(statement.Resource)) {
        expect(resourceText(resource)).toContain('/plugin-simulations/*');
      }
    }
  });

  test('bucket listing is prefix-conditioned to plugin-simulations/ only', () => {
    const listStatements = taskRoleStatements().filter((s) =>
      asArray(s.Action).some((a: string) => /^s3:List/.test(a))
    );
    expect(listStatements.length).toBeGreaterThan(0);
    for (const statement of listStatements) {
      const prefixes = asArray(
        statement.Condition?.StringLike?.['s3:prefix']
      );
      expect(prefixes).toEqual(['plugin-simulations/*']);
    }
  });

  test('no Plugin_Library (workflow-plugins/) path — read or write', () => {
    // The Prepare step copies the plugin .so into the run's prefix
    // precisely so the sandbox task never touches the Plugin_Library.
    const policyText = JSON.stringify(taskRoleStatements());
    expect(policyText).not.toContain('workflow-plugins');
  });

  test('no other portal prefixes (plugin-sources/, plugin-staging/)', () => {
    const policyText = JSON.stringify(taskRoleStatements());
    expect(policyText).not.toContain('plugin-sources');
    expect(policyText).not.toContain('plugin-staging');
  });

  test('no non-S3 data-plane access (DynamoDB, Greengrass, IoT, KMS, ...)', () => {
    for (const statement of taskRoleStatements()) {
      for (const action of asArray(statement.Action)) {
        expect(action).toMatch(/^s3:/);
      }
    }
  });
});

describe('simulator sandbox task definition (Requirements 7.2, task 8.1 dispatch)', () => {
  test('the task definition uses the prefix-scoped task role', () => {
    const definition = simulatorTaskDefinition();
    expect(resourceText(definition.Properties.TaskRoleArn)).toContain(
      taskRoleLogicalId()
    );
  });

  test('the container dispatches to the single-plugin harness (HARNESS_MODE=simulate)', () => {
    const definition = simulatorTaskDefinition();
    const containers = definition.Properties.ContainerDefinitions;
    expect(containers).toHaveLength(1);
    expect(containers[0].Environment).toContainEqual({
      Name: 'HARNESS_MODE',
      Value: 'simulate',
    });
  });
});

describe('simulator state machine timeout wiring (Requirement 7.7)', () => {
  /** The simulator state machine definition as one serialized string. */
  function definitionText(): string {
    const machines = template.findResources(
      'AWS::StepFunctions::StateMachine'
    );
    const simulator: any = Object.values(machines).find((m: any) =>
      JSON.stringify(m.Properties.StateMachineName ?? '').includes(
        'dda-plugin-simulator'
      )
    );
    expect(simulator).toBeDefined();
    return JSON.stringify(simulator.Properties.DefinitionString);
  }

  test('RunSandbox carries the 5-minute task timeout', () => {
    // sfn.Timeout.duration(cdk.Duration.minutes(5)) on the EcsRunTask.
    expect(definitionText()).toContain('TimeoutSeconds\\":300');
  });

  test('a timeout routes to the timeout recorder before the Fail state', () => {
    const text = definitionText();
    // States.Timeout is caught into the record_timeout step, which only
    // marks the run failed-with-timeout — the incrementally flushed
    // partial results in S3 stay untouched (asserted at runtime by the
    // containerized simulate tests and backend test_plugin_simulator.py).
    expect(text).toContain('States.Timeout');
    expect(text).toContain('SimulationRecordTimeout');
    expect(text).toContain('record_timeout');
  });
});
