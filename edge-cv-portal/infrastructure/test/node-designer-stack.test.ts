/**
 * Static infrastructure assertions for the NodeDesignerStack plugin build
 * projects (custom-node-designer task 6.6).
 *
 * Covers the infrastructure half of the builds/auto-packaging integration
 * suite (the runtime half lives in backend/tests/test_plugin_builds.py and
 * test_plugin_components.py):
 *
 * - All five per-Target_Architecture CodeBuild projects plus the lightweight
 *   fetch project exist (Requirement 3.1), captured in a stack snapshot.
 * - Each build project's IAM role is scoped to exactly the plugin-sources
 *   read prefix and its own architecture's staging/Plugin_Library prefixes:
 *   no bucket-wide s3:List*, no other architecture's prefixes
 *   (Requirement 3.2).
 * - No build project has a VpcConfig, so builds have no network path to
 *   portal internals (Requirement 3.2).
 * - Each build role is granted kms:Sign on the plugin signing key
 *   (Requirement 3.1 signing step).
 * - The EventBridge rule delivers terminal build results for all six
 *   projects to the plugin_builds handler (Requirement 3.4 delivery).
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import {
  NodeDesignerStack,
  PLUGIN_BUILD_ARCHITECTURES,
} from '../lib/node-designer-stack';

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

/** All CodeBuild projects in the synthesized stack, keyed by project name. */
function projectsByName(): { [name: string]: any } {
  const projects = template.findResources('AWS::CodeBuild::Project');
  const byName: { [name: string]: any } = {};
  for (const resource of Object.values(projects)) {
    byName[(resource as any).Properties.Name] = resource;
  }
  return byName;
}

/**
 * The per-architecture build-role policies, identified by the staging-prefix
 * resource each contains (`plugin-staging/*\/{arch}/*`). Only the build roles
 * carry an arch-segmented staging prefix, so this uniquely maps policy->arch.
 */
function buildRolePoliciesByArch(): { [arch: string]: any } {
  const policies = template.findResources('AWS::IAM::Policy');
  const byArch: { [arch: string]: any } = {};
  for (const [logicalId, policy] of Object.entries(policies)) {
    const match = JSON.stringify(policy).match(
      /plugin-staging\/\*\/([a-z0-9_]+)\/\*/
    );
    if (!match) {
      continue;
    }
    expect(logicalId).toMatch(/^PluginBuildRole/);
    expect(byArch[match[1]]).toBeUndefined();
    byArch[match[1]] = policy;
  }
  return byArch;
}

/** Flattens a statement's Action / Resource entries to arrays. */
const asArray = (value: any): any[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

/** Serialized form of a resource entry (resolves Fn::Join token structure). */
const resourceText = (resource: any): string =>
  typeof resource === 'string' ? resource : JSON.stringify(resource);

describe('plugin build CodeBuild projects (Requirements 3.1, 3.2)', () => {
  test('all five per-architecture projects plus the fetch project exist', () => {
    const names = Object.keys(projectsByName()).sort();
    const expected = [
      ...PLUGIN_BUILD_ARCHITECTURES.map((arch) => `dda-plugin-build-${arch}`),
      'dda-plugin-fetch',
    ].sort();
    expect(names).toEqual(expected);
  });

  test('no build project has a VpcConfig (no network path to portal internals)', () => {
    for (const [name, project] of Object.entries(projectsByName())) {
      expect({ name, vpcConfig: project.Properties.VpcConfig }).toEqual({
        name,
        vpcConfig: undefined,
      });
    }
  });

  test('all five build projects stack snapshot', () => {
    // Full definition snapshot of the six CodeBuild projects (five
    // per-architecture builds + fetch): environment image, buildspec,
    // source, timeout, and role wiring.
    expect(projectsByName()).toMatchSnapshot();
  });
});

describe('build-project IAM policy static assertions (Requirement 3.2)', () => {
  test('exactly one build-role policy per architecture', () => {
    expect(Object.keys(buildRolePoliciesByArch()).sort()).toEqual(
      [...PLUGIN_BUILD_ARCHITECTURES].sort()
    );
  });

  test.each([...PLUGIN_BUILD_ARCHITECTURES])(
    '%s role: plugin-sources read + own-arch staging/library prefixes only',
    (arch) => {
      const policy = buildRolePoliciesByArch()[arch];
      const statements: any[] =
        policy.Properties.PolicyDocument.Statement;

      // Source read: plugin-sources/* GetObject.
      const getObject = statements.filter(
        (s) =>
          asArray(s.Action).includes('s3:GetObject') &&
          asArray(s.Resource).some((r) =>
            resourceText(r).includes('/plugin-sources/*')
          )
      );
      expect(getObject.length).toBeGreaterThan(0);

      // Staging write for THIS architecture.
      const stagingWrite = statements.some(
        (s) =>
          asArray(s.Action).includes('s3:PutObject') &&
          asArray(s.Resource).some((r) =>
            resourceText(r).includes(`/plugin-staging/*/${arch}/*`)
          )
      );
      expect(stagingWrite).toBe(true);

      // Plugin_Library promotion for THIS architecture.
      const libraryWrite = statements.some(
        (s) =>
          asArray(s.Action).includes('s3:PutObject') &&
          asArray(s.Resource).some((r) =>
            resourceText(r).includes(
              `/workflow-plugins/custom/*/${arch}/*`
            )
          )
      );
      expect(libraryWrite).toBe(true);

      // Every s3 grant is one of exactly: bucket-level List (prefix-
      // conditioned, checked below) or an allowed object prefix.
      const allowedSuffixes = [
        '/plugin-sources/*',
        `/plugin-staging/*/${arch}/*`,
        `/workflow-plugins/custom/*/${arch}/*`,
      ];
      for (const statement of statements) {
        const s3Actions = asArray(statement.Action).filter((a: string) =>
          a.startsWith('s3:')
        );
        if (s3Actions.length === 0) {
          continue;
        }
        const objectActions = s3Actions.filter(
          (a: string) => !/^s3:List/.test(a)
        );
        if (objectActions.length > 0) {
          for (const resource of asArray(statement.Resource)) {
            const text = resourceText(resource);
            expect(
              allowedSuffixes.some((suffix) => text.includes(suffix))
            ).toBe(true);
          }
        }
      }

      // No other architecture's prefix appears anywhere in the policy.
      const policyText = JSON.stringify(policy);
      for (const other of PLUGIN_BUILD_ARCHITECTURES) {
        if (other !== arch) {
          expect(policyText).not.toContain(`/${other}/*`);
        }
      }
    }
  );

  test.each([...PLUGIN_BUILD_ARCHITECTURES])(
    '%s role: no bucket-wide s3:List* (every List is prefix-conditioned)',
    (arch) => {
      const policy = buildRolePoliciesByArch()[arch];
      const statements: any[] =
        policy.Properties.PolicyDocument.Statement;
      const listStatements = statements.filter((s) =>
        asArray(s.Action).some((a: string) => /^s3:List/.test(a))
      );
      expect(listStatements.length).toBeGreaterThan(0);
      for (const statement of listStatements) {
        const prefixes = asArray(
          statement.Condition?.StringLike?.['s3:prefix']
        );
        expect(prefixes.length).toBeGreaterThan(0);
        // Listing is confined to plugin-sources and this arch's staging.
        expect(prefixes.sort()).toEqual(
          ['plugin-sources/*', `plugin-staging/*/${arch}/*`].sort()
        );
      }
    }
  );

  test.each([...PLUGIN_BUILD_ARCHITECTURES])(
    '%s role: kms:Sign granted on the plugin signing key',
    (arch) => {
      const policy = buildRolePoliciesByArch()[arch];
      const statements: any[] =
        policy.Properties.PolicyDocument.Statement;
      const signStatement = statements.find((s) =>
        asArray(s.Action).includes('kms:Sign')
      );
      expect(signStatement).toBeDefined();
      // The grant targets the stack's signing key (GetAtt on its Arn).
      expect(resourceText(signStatement.Resource)).toContain(
        'PluginSigningKey'
      );
    }
  );
});

describe('EventBridge build-result delivery (Requirement 3.4)', () => {
  test('rule matches terminal statuses for all six projects', () => {
    const rules = template.findResources('AWS::Events::Rule');
    const rule: any = Object.values(rules).find(
      (r: any) => r.Properties.Name === 'dda-portal-plugin-build-results'
    );
    expect(rule).toBeDefined();
    const pattern = rule.Properties.EventPattern;
    expect(pattern.source).toEqual(['aws.codebuild']);
    expect(pattern['detail-type']).toEqual(['CodeBuild Build State Change']);
    expect(pattern.detail['build-status'].sort()).toEqual(
      ['FAILED', 'FAULT', 'STOPPED', 'SUCCEEDED', 'TIMED_OUT'].sort()
    );
    expect(pattern.detail['project-name'].sort()).toEqual(
      [
        ...PLUGIN_BUILD_ARCHITECTURES.map((a) => `dda-plugin-build-${a}`),
        'dda-plugin-fetch',
      ].sort()
    );
  });
});
