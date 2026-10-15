/**
 * Property-based tests for the pure context helpers of
 * portal-authorization-and-iam-scoping (`lib/context-helpers.ts`).
 *
 * ZERO CDK imports, so fast-check runs at `{ numRuns: 100 }`; what the
 * helpers produce once synthesized is asserted by
 * portal-authz-scoping-infra.test.ts.
 *
 * Property 7: The data-plane helper conditions every wildcard object and
 *             list grant; the only unconditioned statement on every bucket
 *             is tag discovery with exactly its two actions.
 *             Validates: Requirements 7.1, 7.2
 */

import * as fc from 'fast-check';
import {
  DATA_PLANE_SCOPING,
  DataPlaneScoping,
  PORTAL_MANAGED_TAG_CONDITION,
  dataPlaneGrant,
} from '../lib/context-helpers';

const RUNS = { numRuns: 100 };
const ANY_BUCKET = 'arn:aws:s3:::*';
const SCOPINGS: DataPlaneScoping[] = ['tag', 'tag-objects-only', 'allowlist'];

interface EmittedStatement {
  name: string;
  actions: string[];
  resources: string[];
  conditions?: unknown;
}

/** Every statement a stack builds from one grant (the prefix grant as DeleteObject). */
function emitted(scoping: DataPlaneScoping, allowlist: string[] | undefined, prefix: string): EmittedStatement[] {
  const grant = dataPlaneGrant(allowlist, scoping);
  return [
    { name: 'bucketLevel', ...grant.bucketLevel },
    { name: 'objectLevel', ...grant.objectLevel },
    { name: 'objectPrefix', actions: ['s3:DeleteObject'], ...grant.objectPrefix(prefix) },
    { name: 'tagDiscovery', ...grant.tagDiscovery },
  ];
}

// S3 bucket names: 3-63 characters of lower-case letters, digits, '-' and
// '.', starting and ending with a letter or digit.
const alnumArb = fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789'.split(''));
const bucketNameArb = fc
  .tuple(
    alnumArb,
    fc.array(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789-.'.split('')), {
      minLength: 1,
      maxLength: 30,
    }),
    alnumArb,
  )
  .map(([first, middle, last]) => first + middle.join('') + last);

// An allowlist entry in each spelling the context value accepts: the bare
// name, its ARN, and the ARN with a trailing '/' or '/*'.
const allowlistEntryArb = fc
  .tuple(bucketNameArb, fc.constantFrom('bare', 'arn', 'arn/', 'arn/*'))
  .map(([name, form]) => ({
    name,
    entry: form === 'bare' ? name : `arn:aws:s3:::${name}${form.slice(3)}`,
  }));
const allowlistArb = fc.array(allowlistEntryArb, { minLength: 1, maxLength: 6 });

const prefixArb = fc
  .array(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789-'.split('')), {
    minLength: 1,
    maxLength: 16,
  })
  .map((chars) => chars.join(''));

const isWildcardObjectResource = (resource: string) => resource.startsWith(`${ANY_BUCKET}/`);

describe('Feature: portal-authorization-and-iam-scoping, Property 7: The data-plane helper conditions every wildcard object and list grant; the only unconditioned statement on every bucket is tag discovery with exactly its two actions', () => {
  it('ships the tag scoping', () => {
    expect(DATA_PLANE_SCOPING).toBe('tag');
    expect(PORTAL_MANAGED_TAG_CONDITION).toEqual({
      StringEquals: { 'aws:ResourceTag/dda-portal:managed': 'true' },
    });
  });

  it('conditions every object statement on a wildcard bucket under tag and tag-objects-only (Validates: Requirements 7.1, 7.2)', () => {
    fc.assert(
      fc.property(
        fc.constantFrom<DataPlaneScoping>('tag', 'tag-objects-only'),
        allowlistArb,
        prefixArb,
        (scoping, entries, prefix) => {
          const statements = emitted(scoping, entries.map((e) => e.entry), prefix);
          const wildcardObjects = statements.filter((s) => s.resources.some(isWildcardObjectResource));
          // objectLevel and objectPrefix both reach every bucket's objects.
          expect(wildcardObjects.map((s) => s.name).sort()).toEqual(['objectLevel', 'objectPrefix']);
          for (const statement of wildcardObjects) {
            expect(statement.conditions).toEqual(PORTAL_MANAGED_TAG_CONDITION);
          }
          const prefixed = statements.find((s) => s.name === 'objectPrefix')!;
          expect(prefixed.resources).toEqual([`${ANY_BUCKET}/${prefix}/*`]);
        },
      ),
      RUNS,
    );
  });

  it('conditions s3:ListBucket and s3:GetBucketLocation on every bucket under tag, ignoring the allowlist (Validates: Requirement 7.2)', () => {
    fc.assert(
      fc.property(fc.option(allowlistArb, { nil: undefined }), prefixArb, (entries, prefix) => {
        const allowlist = entries?.map((e) => e.entry);
        const statements = emitted('tag', allowlist, prefix);
        const listing = statements.filter(
          (s) =>
            (s.actions.includes('s3:ListBucket') || s.actions.includes('s3:GetBucketLocation')) &&
            s.resources.includes(ANY_BUCKET),
        );
        expect(listing.map((s) => s.name)).toEqual(['bucketLevel']);
        expect(listing[0].conditions).toEqual(PORTAL_MANAGED_TAG_CONDITION);
        expect(listing[0].actions).toEqual(['s3:ListBucket', 's3:GetBucketLocation']);
        // The allowlist never narrows or widens the tag scoping.
        expect(statements).toEqual(emitted('tag', undefined, prefix));
      }),
      RUNS,
    );
  });

  it('leaves tag discovery as the only unconditioned statement on every bucket, in every scoping, with exactly its two actions (Validates: Requirement 7.2)', () => {
    fc.assert(
      fc.property(fc.constantFrom(...SCOPINGS), allowlistArb, prefixArb, (scoping, entries, prefix) => {
        const statements = emitted(scoping, entries.map((e) => e.entry), prefix);
        const unconditionedOnEveryBucket = statements.filter(
          (s) => s.conditions === undefined && s.resources.includes(ANY_BUCKET),
        );
        expect(unconditionedOnEveryBucket.map((s) => s.name)).toEqual(['tagDiscovery']);
        expect([...unconditionedOnEveryBucket[0].actions].sort()).toEqual([
          's3:GetBucketABAC',
          's3:GetBucketTagging',
        ]);
        expect(unconditionedOnEveryBucket[0].resources).toEqual([ANY_BUCKET]);
        // No unconditioned statement reaches the objects of every bucket.
        expect(
          statements.filter((s) => s.conditions === undefined && s.resources.some(isWildcardObjectResource)),
        ).toEqual([]);
      }),
      RUNS,
    );
  });

  it('names the allowlist as canonical bucket ARNs under tag-objects-only and allowlist (Validates: Requirement 7.2)', () => {
    fc.assert(
      fc.property(
        fc.constantFrom<DataPlaneScoping>('tag-objects-only', 'allowlist'),
        allowlistArb,
        prefixArb,
        (scoping, entries, prefix) => {
          const grant = dataPlaneGrant(entries.map((e) => e.entry), scoping);
          const expectedArns = entries.map((e) => `arn:aws:s3:::${e.name}`);
          expect(grant.bucketLevel.resources).toEqual(expectedArns);
          expect(grant.bucketLevel.conditions).toBeUndefined();
          for (const resource of grant.bucketLevel.resources) {
            expect(resource).toMatch(/^arn:aws:s3:::[a-z0-9][a-z0-9.-]*[a-z0-9]$/);
            expect(resource).not.toContain('*');
          }
          if (scoping === 'allowlist') {
            expect(grant.objectLevel.resources).toEqual(expectedArns.map((arn) => `${arn}/*`));
            expect(grant.objectLevel.conditions).toBeUndefined();
            expect(grant.objectPrefix(prefix)).toEqual({
              resources: expectedArns.map((arn) => `${arn}/${prefix}/*`),
            });
          }
        },
      ),
      RUNS,
    );
  });

  it('throws on an empty allowlist under tag-objects-only and allowlist, never falling back to every bucket (Validates: Requirement 7.2)', () => {
    const blankArb = fc
      .array(fc.constantFrom('', ' ', '\t'), { maxLength: 4 })
      .map((chars) => chars.join(''));
    fc.assert(
      fc.property(
        fc.constantFrom<DataPlaneScoping>('tag-objects-only', 'allowlist'),
        fc.oneof(
          fc.constant<string[] | undefined>(undefined),
          fc.constant<string[] | undefined>([]),
          fc.array(blankArb, { minLength: 1, maxLength: 3 }),
        ),
        (scoping, allowlist) => {
          expect(() => dataPlaneGrant(allowlist, scoping)).toThrow(/non-empty bucket allowlist/);
        },
      ),
      RUNS,
    );
    // The tag scoping needs no allowlist.
    expect(() => dataPlaneGrant(undefined, 'tag')).not.toThrow();
    expect(() => dataPlaneGrant([], 'tag')).not.toThrow();
  });

  it('returns fresh condition objects, so one stack cannot change another statement through them', () => {
    const first = dataPlaneGrant(undefined, 'tag');
    const second = dataPlaneGrant(undefined, 'tag');
    expect(first.objectLevel.conditions).not.toBe(PORTAL_MANAGED_TAG_CONDITION);
    expect(first.objectLevel.conditions).not.toBe(second.objectLevel.conditions);
    expect(first.objectLevel.conditions).not.toBe(first.bucketLevel.conditions);
  });
});
