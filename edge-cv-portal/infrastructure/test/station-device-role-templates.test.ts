/**
 * Station device-role templates (portal-authorization-and-iam-scoping,
 * design I6 "Design"; task 6.2; Requirement 8.3).
 *
 * The three station files that write S3 grants on the device token-exchange
 * role — station_install/setup_station.sh (inline GreengrassComponentS3Access),
 * station_install/create-edge-device-iam-role.sh (INLINE_POLICY heredoc) and
 * station_install/edge-device-iam-policy.json — carry the S3 statement set of
 * the account stack's DDAPortalComponentAccessPolicy (component bucket read,
 * bucket-pattern read, inference-results upload) minus the *-dda-* substring
 * pattern, compared by Actions and Resource patterns with the account and
 * region of the component bucket name normalized; edge-device-iam-policy.json
 * additionally carries its greengrass-component-artifacts-* read and nothing
 * else.
 *
 * Recorded deviation from Requirement 8.3: the pattern read is limited to
 * dda-* because the repository IAM audit (test/backend-test/security/
 * iam_audit.py) refuses the *-dda-* substring pattern in the station JSON
 * templates, as deploy-account-role.sh's copy of the policy already is. A
 * *-dda-* bucket stays device-readable on the setup_station.sh path through the
 * attached DDAPortalComponentAccessPolicy (pinned below); the 6.1 Device Access
 * Document treats it as template-covered and writes no statement for it.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { UseCaseAccountStack } from '../lib/usecase-account-stack';

const REPO_ROOT = path.join(__dirname, '..', '..', '..');
const STATION = path.join(REPO_ROOT, 'station_install');
// Fixture account id, assembled so no identifier-shaped literal lands in the source.
const FIXTURE_ACCOUNT = '1'.repeat(12);

/** The substring pattern the station templates leave to the attached managed policy. */
const SUBSTRING_PATTERN = ['arn:aws:s3:::*-dda-*', 'arn:aws:s3:::*-dda-*/*'];

/** The Greengrass public component artifacts read edge-device-iam-policy.json keeps. */
const ARTIFACTS_READ = {
  actions: ['s3:GetObject', 's3:GetObjectVersion'],
  resources: ['arn:aws:s3:::greengrass-component-artifacts-*/*'],
};

interface Canonical {
  actions: string[];
  resources: string[];
}

const asArray = (value: unknown): unknown[] =>
  value === undefined ? [] : Array.isArray(value) ? value : [value];

/** The component bucket's name carries the region and the portal account; the station files use the pattern. */
function normalizeResource(resource: unknown): string {
  if (typeof resource !== 'string') {
    throw new Error(`unresolved resource ${JSON.stringify(resource)}`);
  }
  return resource.replace(/^arn:aws:s3:::dda-component-[^/]+/, 'arn:aws:s3:::dda-component-*');
}

function canonical(statement: any): Canonical {
  expect(statement.Effect).toBe('Allow');
  expect(statement.Condition).toBeUndefined();
  return {
    actions: (asArray(statement.Action) as string[]).slice().sort(),
    resources: [...new Set(asArray(statement.Resource).map(normalizeResource))].sort(),
  };
}

/** The S3 statements of a policy document, canonical and sorted. */
function s3Statements(document: any): Canonical[] {
  return (document.Statement as any[])
    .filter((s) => (asArray(s.Action) as string[]).some((a) => a.startsWith('s3:')))
    .map(canonical)
    .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

function readStation(file: string): string {
  return fs.readFileSync(path.join(STATION, file), 'utf-8');
}

/** create-edge-device-iam-role.sh: the INLINE_POLICY heredoc body. */
function edgeRoleInlinePolicy(): any {
  const text = readStation('create-edge-device-iam-role.sh');
  const start = text.indexOf("INLINE_POLICY=$(cat <<'EOF'\n");
  expect(start).toBeGreaterThanOrEqual(0);
  const bodyStart = text.indexOf('\n', start) + 1;
  const end = text.indexOf('\nEOF\n', bodyStart);
  expect(end).toBeGreaterThan(bodyStart);
  return JSON.parse(text.slice(bodyStart, end));
}

/** setup_station.sh: the GreengrassComponentS3Access --policy-document of the double-quoted run_cmd string. */
function setupStationInlinePolicy(): any {
  const text = readStation('setup_station.sh');
  const anchor = text.indexOf('--policy-name GreengrassComponentS3Access');
  expect(anchor).toBeGreaterThanOrEqual(0);
  const open = text.indexOf("--policy-document '", anchor) + "--policy-document '".length;
  const close = text.indexOf(`}'"; then`, open) + 1;
  expect(close).toBeGreaterThan(open);
  // Inside the double-quoted command string the JSON quotes are escaped.
  return JSON.parse(text.slice(open, close).replace(/\\"/g, '"'));
}

let componentAccessS3: Canonical[];
/** The account-stack set minus the *-dda-* substring pattern: what the station templates carry. */
let stationS3: Canonical[];

/** IAM resource match (`*` spans any characters, `/` included). */
function resourceMatches(pattern: string, arn: string): boolean {
  const escape = (part: string) => part.replace(/[.+?^$()|[\]{}\\]/g, '\\$&');
  return new RegExp('^' + pattern.split('*').map(escape).join('.*') + '$').test(arn);
}

/** Does a canonical statement set allow `action` on `arn`? */
function allows(set: Canonical[], action: string, arn: string): boolean {
  return set.some((s) => s.actions.includes(action) && s.resources.some((r) => resourceMatches(r, arn)));
}

beforeAll(() => {
  const app = new cdk.App();
  const stack = new UseCaseAccountStack(app, 'DDAPortalUseCaseAccountStack', {
    portalAccountId: FIXTURE_ACCOUNT,
    env: { account: FIXTURE_ACCOUNT, region: 'us-east-1' },
  });
  const policies = Object.values(
    Template.fromStack(stack).findResources('AWS::IAM::ManagedPolicy', {
      Properties: { ManagedPolicyName: 'DDAPortalComponentAccessPolicy' },
    }),
  ) as any[];
  expect(policies).toHaveLength(1);
  componentAccessS3 = s3Statements(policies[0].Properties.PolicyDocument);
  stationS3 = componentAccessS3.map((s) => ({
    actions: s.actions,
    resources: s.resources.filter((r) => !SUBSTRING_PATTERN.includes(r)),
  }));
}, 120_000);

describe('DDAPortalComponentAccessPolicy S3 statement set', () => {
  test('is the component read, the pattern read and the inference-results upload', () => {
    expect(componentAccessS3).toEqual(
      [
        {
          actions: ['s3:GetBucketLocation', 's3:GetObject'],
          resources: ['arn:aws:s3:::dda-component-*', 'arn:aws:s3:::dda-component-*/*'],
        },
        {
          actions: ['s3:GetBucketLocation', 's3:GetObject', 's3:HeadObject'],
          resources: [
            'arn:aws:s3:::*-dda-*',
            'arn:aws:s3:::*-dda-*/*',
            'arn:aws:s3:::dda-*',
            'arn:aws:s3:::dda-*/*',
          ],
        },
        {
          actions: ['s3:GetBucketLocation', 's3:GetObject', 's3:PutObject', 's3:PutObjectTagging'],
          resources: ['arn:aws:s3:::dda-inference-results-*', 'arn:aws:s3:::dda-inference-results-*/*'],
        },
      ].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
    );
  });
});

describe('station templates', () => {
  test('the station set is the account stack set minus exactly the *-dda-* substring pattern', () => {
    const removed = componentAccessS3.flatMap((s) => s.resources).filter((r) => SUBSTRING_PATTERN.includes(r));
    expect(removed.sort()).toEqual([...SUBSTRING_PATTERN].sort());
    expect(stationS3.flatMap((s) => s.resources).filter((r) => r.includes('*-dda-'))).toEqual([]);
  });

  test('setup_station.sh GreengrassComponentS3Access carries exactly the station set', () => {
    const document = setupStationInlinePolicy();
    expect(document.Version).toBe('2012-10-17');
    expect(s3Statements(document)).toEqual(stationS3);
    // The inline policy holds S3 statements only.
    expect(s3Statements(document)).toHaveLength(document.Statement.length);
  });

  test('create-edge-device-iam-role.sh INLINE_POLICY carries exactly the station set', () => {
    expect(s3Statements(edgeRoleInlinePolicy())).toEqual(stationS3);
  });

  test('edge-device-iam-policy.json carries the station set plus the artifacts read only', () => {
    const document = JSON.parse(readStation('edge-device-iam-policy.json'));
    const statements = s3Statements(document);
    const artifacts = statements.filter((s) => JSON.stringify(s) === JSON.stringify(ARTIFACTS_READ));
    expect(artifacts).toHaveLength(1);
    expect(statements.filter((s) => JSON.stringify(s) !== JSON.stringify(ARTIFACTS_READ))).toEqual(
      stationS3,
    );
  });

  test('no station S3 statement reaches every bucket', () => {
    const documents = [
      setupStationInlinePolicy(),
      edgeRoleInlinePolicy(),
      JSON.parse(readStation('edge-device-iam-policy.json')),
    ];
    for (const document of documents) {
      for (const statement of s3Statements(document)) {
        for (const resource of statement.resources) {
          expect(['*', 'arn:aws:s3:::*', 'arn:aws:s3:::*/*']).not.toContain(resource);
        }
      }
    }
  });
});

describe('a *-dda-* bucket on the setup_station.sh path', () => {
  // A use-case bucket named with the substring pattern but not the dda- prefix.
  const BUCKET = 'line3-dda-images';
  const OBJECT = `arn:aws:s3:::${BUCKET}/models/model.tar.gz`;

  test('is not read through the station inline policy, before or after the convergence', () => {
    expect(allows(s3Statements(setupStationInlinePolicy()), 's3:GetObject', OBJECT)).toBe(false);
    // The installer's inline policy before task 6.2 read the component bucket only.
    const before: Canonical[] = [
      {
        actions: ['s3:GetObject', 's3:GetObjectVersion'],
        resources: ['arn:aws:s3:::dda-component-*/*', 'arn:aws:s3:::dda-component-us-east-1-*/*'],
      },
    ];
    expect(allows(before, 's3:GetObject', OBJECT)).toBe(false);
  });

  test('is read through the DDAPortalComponentAccessPolicy the installer attaches', () => {
    expect(allows(componentAccessS3, 's3:GetObject', OBJECT)).toBe(true);
    expect(allows(componentAccessS3, 's3:GetBucketLocation', `arn:aws:s3:::${BUCKET}`)).toBe(true);
    const text = readStation('setup_station.sh');
    expect(text).toContain('DDA_POLICY_ARN="arn:aws:iam::${aws_account_id}:policy/DDAPortalComponentAccessPolicy"');
    expect(text).toContain(
      'run_cmd "aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn $DDA_POLICY_ARN"',
    );
  });

  test('gets no Device Access Document statement (the template is relied on)', () => {
    const grants = fs.readFileSync(
      path.join(REPO_ROOT, 'edge-cv-portal', 'backend', 'functions', 'device_grants.py'),
      'utf-8',
    );
    expect(grants).toContain("TEMPLATE_BUCKET_PATTERNS = ('dda-*', '*-dda-*')");
  });

  test('a dda-* bucket is read through every station template', () => {
    const object = 'arn:aws:s3:::dda-line3-images/models/model.tar.gz';
    for (const document of [
      setupStationInlinePolicy(),
      edgeRoleInlinePolicy(),
      JSON.parse(readStation('edge-device-iam-policy.json')),
    ]) {
      expect(allows(s3Statements(document), 's3:GetObject', object)).toBe(true);
    }
  });
});
