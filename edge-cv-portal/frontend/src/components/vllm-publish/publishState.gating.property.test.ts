/**
 * **Feature: vllm-package-publish-gui, Property 1: Action gating and labeling derive from record and role**
 *
 * For any model record and user role, `derivePanelState` SHALL make the
 * Package_Publish_Action visible if and only if the record is a vLLM record;
 * when visible, it SHALL be enabled (absent an in-flight session) if and only
 * if the role is in `VLLM_PACKAGE_ROLES`, SHALL carry the permission message
 * exactly when the role is not permitted, and SHALL carry the re-publish
 * label with the next-version note if and only if the record has a
 * `published_component`. Non-vLLM records SHALL yield no vLLM action, no
 * vLLM state sections, and no polling-related output.
 *
 * **Validates: Requirements 1.1, 1.3, 1.4, 1.6, 5.1, 5.4**
 *
 * The oracles are computed independently of the module's helpers: role
 * permission is re-derived from the DataScientist-and-above hierarchy the
 * backend's `check_user_access` enforces (not via `canPackageVllm`), and
 * in-flight status is re-derived from the session kind union (`requesting`,
 * `confirming`, `polling`).
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { VllmPublishedComponent } from '../../services/api';
import { UserRole } from '../../types';
import {
  derivePanelState,
  nextComponentVersion,
  PERMISSION_MESSAGE,
  VLLM_PACKAGE_ROLES,
  type PackagedComponentEntry,
  type PublishSession,
  type SessionError,
  type VllmPublishRecord,
} from './publishState';

// ------------------------------------------------------------- generators

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

/** Any role plus the signed-out / role-less case. */
const roleArb: fc.Arbitrary<UserRole | undefined> = fc.constantFrom(
  ...ALL_ROLES,
  undefined
);

const versionArb = fc
  .integer({ min: 1, max: 40 })
  .map((major) => `${major}.0.0`);

const publishedComponentArb: fc.Arbitrary<VllmPublishedComponent> = fc.record({
  component_name: fc
    .stringMatching(/^[a-z0-9-]{1,20}$/)
    .map((s) => `model-vllm-${s}`),
  component_version: versionArb,
  supported_architectures: fc.array(
    fc.constantFrom('arm64_jp6', 'arm64_jp5', 'x86_64'),
    { maxLength: 3 }
  ),
  runtime: fc.constant('vllm'),
  component_arns: fc.dictionary(
    fc.constantFrom('jetson-xavier-jp6', 'jetson-orin-jp6'),
    fc.string({ minLength: 1, maxLength: 40 }),
    { maxKeys: 2 }
  ),
  published_at: fc.integer({ min: 1_600_000_000_000, max: 1_900_000_000_000 }),
});

const packagedEntryArb: fc.Arbitrary<PackagedComponentEntry> = fc
  .record({
    target: fc.constantFrom('jetson-xavier-jp6', 'jetson-orin-jp6', 'x86_64'),
    status: fc.constantFrom('packaged', 'failed', 'error', 'in_progress'),
    error: fc.option(fc.string({ minLength: 1, maxLength: 40 }), {
      nil: undefined,
    }),
    supported_architectures: fc.option(
      fc.array(fc.constantFrom('arm64_jp6', 'arm64_jp5'), { maxLength: 2 }),
      { nil: undefined }
    ),
  })
  .map((entry) =>
    Object.fromEntries(
      Object.entries(entry).filter(([, value]) => value !== undefined)
    ) as unknown as PackagedComponentEntry
  );

/** Records across vLLM and non-vLLM model types, with and without
 *  packaged/published state. */
const recordArb: fc.Arbitrary<VllmPublishRecord> = fc
  .record({
    model_type: fc.constantFrom('vllm', 'trained', 'imported', 'yolo', ''),
    packaged_components: fc.option(
      fc.array(packagedEntryArb, { maxLength: 4 }),
      { nil: undefined }
    ),
    published_component: fc.option(publishedComponentArb, { nil: undefined }),
  })
  .map((record) =>
    Object.fromEntries(
      Object.entries(record).filter(([, value]) => value !== undefined)
    ) as unknown as VllmPublishRecord
  );

const baselineArb: fc.Arbitrary<string | null> = fc.option(versionArb, {
  nil: null,
});

const sessionErrorArb: fc.Arbitrary<SessionError> = fc.record({
  message: fc.string({ minLength: 1, maxLength: 40 }),
  source: fc.constantFrom<SessionError['source']>(
    'package',
    'publish-retry',
    'record'
  ),
});

/** Every session kind of the state machine, in-flight and terminal alike. */
const sessionArb: fc.Arbitrary<PublishSession> = fc.oneof(
  fc.constant<PublishSession>({ kind: 'idle' }),
  fc.record({
    kind: fc.constant<'requesting'>('requesting'),
    action: fc.constantFrom<'package' | 'publish-retry'>(
      'package',
      'publish-retry'
    ),
    baseline: baselineArb,
  }),
  fc.record({
    kind: fc.constant<'confirming'>('confirming'),
    baseline: baselineArb,
  }),
  fc
    .record({
      baseline: baselineArb,
      startedAt: fc.integer({ min: 0, max: 2_000_000_000_000 }),
    })
    .map<PublishSession>(({ baseline, startedAt }) => ({
      kind: 'polling',
      baseline,
      startedAt,
      deadline: startedAt + 300_000,
    })),
  publishedComponentArb.map<PublishSession>((component) => ({
    kind: 'published',
    component,
  })),
  fc.constant<PublishSession>({ kind: 'timed-out' }),
  sessionErrorArb.map<PublishSession>((error) => ({ kind: 'failed', error }))
);

// ---------------------------------------------------------------- oracles

/** Independent role oracle: DataScientist and above in the backend's
 *  Viewer < Operator < DataScientist < UseCaseAdmin < PortalAdmin
 *  hierarchy (does not call canPackageVllm). */
function oraclePermitted(role: UserRole | undefined): boolean {
  return (
    role === 'DataScientist' ||
    role === 'UseCaseAdmin' ||
    role === 'PortalAdmin'
  );
}

/** Independent in-flight oracle over the session kind union. */
function oracleInFlight(session: PublishSession): boolean {
  return (
    session.kind === 'requesting' ||
    session.kind === 'confirming' ||
    session.kind === 'polling'
  );
}

// ------------------------------------------------------------------ tests

describe('Property 1: Action gating and labeling derive from record and role', () => {
  it('the action is visible iff the record is a vLLM record', () => {
    fc.assert(
      fc.property(recordArb, roleArb, sessionArb, (record, role, session) => {
        const panel = derivePanelState(record, role, session);
        expect(panel.visible).toBe(record.model_type === 'vllm');
      }),
      { numRuns: 100 }
    );
  });

  it(
    'when visible, the action is enabled iff the role is in ' +
      'VLLM_PACKAGE_ROLES and no session is in flight, and carries the ' +
      'permission message exactly when the role is not permitted',
    () => {
      const vllmRecordArb = recordArb.map((record) => ({
        ...record,
        model_type: 'vllm',
      }));
      fc.assert(
        fc.property(
          vllmRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            const permitted = oraclePermitted(role);
            const inFlight = oracleInFlight(session);

            // The exported roles list matches the hierarchy oracle.
            expect(permitted).toBe(
              role !== undefined && VLLM_PACKAGE_ROLES.includes(role)
            );

            // Enabled iff permitted, absent an in-flight session (Req 1.1,
            // 1.5, 1.6).
            expect(panel.action.enabled).toBe(permitted && !inFlight);

            // Permission message exactly when not permitted (Req 1.6).
            if (permitted) {
              expect(panel.action.permissionMessage).toBeUndefined();
            } else {
              expect(panel.action.permissionMessage).toBe(PERMISSION_MESSAGE);
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'when visible, the action carries the re-publish label with the ' +
      'next-version note and confirmation flag iff the record has a ' +
      'published_component',
    () => {
      const vllmRecordArb = recordArb.map((record) => ({
        ...record,
        model_type: 'vllm',
      }));
      fc.assert(
        fc.property(
          vllmRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            const published = record.published_component;

            if (published) {
              // Re-publish labeling and next-version note (Req 1.4, 1.7).
              expect(panel.action.label).toBe('Re-publish Component');
              expect(panel.action.requiresConfirmation).toBe(true);
              expect(panel.action.republishNote).toBeDefined();
              expect(panel.action.republishNote).toContain(
                nextComponentVersion(published.component_version)
              );
              expect(panel.action.republishNote).toContain(
                'next component version'
              );
            } else {
              // First publish labeling (Req 1.1).
              expect(panel.action.label).toBe('Package & Publish');
              expect(panel.action.requiresConfirmation).toBe(false);
              expect(panel.action.republishNote).toBeUndefined();
            }
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'non-vLLM records yield no vLLM action, no state sections, and no ' +
      'polling-related output',
    () => {
      const nonVllmRecordArb = recordArb.filter(
        (record) => record.model_type !== 'vllm'
      );
      fc.assert(
        fc.property(
          nonVllmRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);

            // No visible action for the record (Req 1.3, 5.1, 5.4).
            expect(panel.visible).toBe(false);
            expect(panel.action.enabled).toBe(false);
            expect(panel.action.loading).toBe(false);

            // No vLLM state sections or retry action (Req 5.1, 5.4).
            expect(panel.publishRetry).toBeUndefined();
            expect(panel.packagedSection).toBeUndefined();
            expect(panel.publishedSection).toBeUndefined();

            // No polling-related or session banner output (Req 5.4).
            expect(panel.progress).toBeUndefined();
            expect(panel.success).toBeUndefined();
            expect(panel.pending).toBeUndefined();
            expect(panel.error).toBeUndefined();
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

/**
 * **Feature: vllm-package-publish-gui, Property 2: Record state sections derive solely from the record**
 *
 * For any model record and any session state, the packaged-state section
 * SHALL be present (listing each entry's target and status, including
 * recorded failure info for failed entries) if and only if the record has
 * at least one `packaged_components` entry; the published-state section
 * SHALL be present (with component name, version, publish timestamp, and
 * component ARNs) if and only if the record has a `published_component`;
 * and the publish-only retry action SHALL be offered if and only if the
 * record has at least one successfully packaged entry and no
 * `published_component`. Records with neither SHALL show neither section.
 *
 * **Validates: Requirements 3.3, 4.1, 4.2, 4.3, 4.5, 4.6**
 *
 * Oracles are computed independently of the module's helpers: packaged
 * success is re-derived from the `status === 'packaged'` convention that
 * packaging.py writes (not via `isPackagedEntrySuccess`), and section
 * presence is re-derived directly from the raw record fields.
 */

describe('Property 2: Record state sections derive solely from the record', () => {
  const vllmRecordArb = recordArb.map((record) => ({
    ...record,
    model_type: 'vllm',
  }));

  it(
    'the packaged-state section is present iff the record has at least ' +
      'one packaged entry, listing each entry’s target, status, and ' +
      'recorded failure info',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            const entries = record.packaged_components ?? [];

            if (entries.length === 0) {
              // No entries → no packaged section (Req 4.1, 4.5).
              expect(panel.packagedSection).toBeUndefined();
              return;
            }

            // One row per recorded entry, in record order (Req 4.1, 4.2).
            expect(panel.packagedSection).toBeDefined();
            expect(panel.packagedSection).toHaveLength(entries.length);
            entries.forEach((entry, i) => {
              const row = panel.packagedSection![i];
              expect(row.target).toBe(entry.target);
              expect(row.status).toBe(entry.status);
              // Recorded failure info surfaces on the row (Req 4.6).
              if (entry.error) {
                expect(row.error).toBe(entry.error);
              } else {
                expect(row.error).toBeUndefined();
              }
            });
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'the published-state section is present iff the record has a ' +
      'published_component, carrying name, version, timestamp, and ARNs ' +
      'straight off the record',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            const published = record.published_component;

            if (!published) {
              // No published_component → no published section (Req 4.1,
              // 4.5).
              expect(panel.publishedSection).toBeUndefined();
              return;
            }

            // Fields derive solely from the record's map (Req 4.1, 4.3).
            expect(panel.publishedSection).toBeDefined();
            expect(panel.publishedSection!.componentName).toBe(
              published.component_name
            );
            expect(panel.publishedSection!.componentVersion).toBe(
              published.component_version
            );
            expect(panel.publishedSection!.publishedAt).toBe(
              published.published_at
            );
            expect(panel.publishedSection!.componentArns).toEqual(
              published.component_arns
            );
            expect(panel.publishedSection!.supportedArchitectures).toEqual(
              published.supported_architectures
            );
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'the publish-only retry action is offered iff the record has at ' +
      'least one successfully packaged entry and no published_component',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);

            // Independent oracle: packaging.py writes 'packaged' on
            // success; anything else is not a success status.
            const hasPackagedSuccess = (
              record.packaged_components ?? []
            ).some((entry) => entry.status === 'packaged');
            const shouldOffer =
              hasPackagedSuccess &&
              record.published_component === undefined;

            // Presence derives solely from the record (Req 3.3, 4.2).
            expect(panel.publishRetry !== undefined).toBe(shouldOffer);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'records with neither packaged entries nor a published_component ' +
      'show neither section and no retry action',
    () => {
      const bareRecordArb = vllmRecordArb.map((record) => {
        const bare = { ...record };
        delete bare.packaged_components;
        delete bare.published_component;
        return bare;
      });
      fc.assert(
        fc.property(
          bareRecordArb,
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            // Req 4.5: nothing record-derived to display.
            expect(panel.packagedSection).toBeUndefined();
            expect(panel.publishedSection).toBeUndefined();
            expect(panel.publishRetry).toBeUndefined();
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'for the same record, the sections and retry presence are identical ' +
      'across any two session states',
    () => {
      fc.assert(
        fc.property(
          vllmRecordArb,
          roleArb,
          sessionArb,
          sessionArb,
          (record, role, sessionA, sessionB) => {
            const panelA = derivePanelState(record, role, sessionA);
            const panelB = derivePanelState(record, role, sessionB);

            // Record-derived output depends ONLY on the record (Req 4.1):
            // no session state may add, remove, or alter the sections.
            expect(panelA.packagedSection).toEqual(panelB.packagedSection);
            expect(panelA.publishedSection).toEqual(panelB.publishedSection);
            expect(panelA.publishRetry !== undefined).toBe(
              panelB.publishRetry !== undefined
            );
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});

/**
 * **Feature: vllm-package-publish-gui, Property 3: Supported architectures rendering**
 *
 * For any published component write-back, the Supported Architectures
 * derivation SHALL produce exactly one badge value per entry of a
 * non-empty `supported_architectures` list, and SHALL fall back to the
 * placeholder (while the published section still shows component name
 * and version) when the list is empty.
 *
 * **Validates: Requirements 2.4, 2.8**
 *
 * The badge derivation under test is the `publishedSection`'s
 * `supportedArchitectures` list that `derivePanelState` exposes for the
 * Supported_Architectures_Section: a non-empty write-back list must map
 * one-to-one (same values, same order, duplicates preserved) onto badge
 * values, and an empty list must stay empty — the section's signal to
 * retain the placeholder — while the published section still carries the
 * component name and version straight off the record.
 */

describe('Property 3: Supported architectures rendering', () => {
  /** Architecture values the packaging/publish backends record. */
  const architectureArb = fc.constantFrom('arm64_jp6', 'arm64_jp5', 'x86_64');

  /** Published component with a guaranteed non-empty architecture list. */
  const publishedNonEmptyArchsArb: fc.Arbitrary<VllmPublishedComponent> = fc
    .tuple(
      publishedComponentArb,
      fc.array(architectureArb, { minLength: 1, maxLength: 4 })
    )
    .map(([component, supported_architectures]) => ({
      ...component,
      supported_architectures,
    }));

  /** Published component whose write-back carries an empty list. */
  const publishedEmptyArchsArb: fc.Arbitrary<VllmPublishedComponent> =
    publishedComponentArb.map((component) => ({
      ...component,
      supported_architectures: [],
    }));

  /** vLLM record carrying the given published component write-back. */
  const vllmRecordWith = (
    publishedArb: fc.Arbitrary<VllmPublishedComponent>
  ): fc.Arbitrary<VllmPublishRecord> =>
    fc
      .tuple(recordArb, publishedArb)
      .map(([record, published_component]) => ({
        ...record,
        model_type: 'vllm',
        published_component,
      }));

  it(
    'a non-empty supported_architectures write-back yields exactly one ' +
      'badge value per entry, matching values and order',
    () => {
      fc.assert(
        fc.property(
          vllmRecordWith(publishedNonEmptyArchsArb),
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            const written =
              record.published_component!.supported_architectures;

            // The published section renders (Req 2.4) …
            expect(panel.publishedSection).toBeDefined();
            const badges = panel.publishedSection!.supportedArchitectures;

            // … with exactly one badge value per write-back entry:
            // same count, same values, same order, duplicates kept.
            expect(badges).toHaveLength(written.length);
            expect(badges).toEqual(written);
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'an empty supported_architectures write-back yields no badge values ' +
      '(placeholder fallback) while the published section still shows the ' +
      'component name and version',
    () => {
      fc.assert(
        fc.property(
          vllmRecordWith(publishedEmptyArchsArb),
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            const published = record.published_component!;

            // No badge values → the section retains the placeholder
            // (Req 2.8).
            expect(panel.publishedSection).toBeDefined();
            expect(panel.publishedSection!.supportedArchitectures).toEqual(
              []
            );

            // The published section still carries name and version
            // (Req 2.8).
            expect(panel.publishedSection!.componentName).toBe(
              published.component_name
            );
            expect(panel.publishedSection!.componentVersion).toBe(
              published.component_version
            );
          }
        ),
        { numRuns: 100 }
      );
    }
  );

  it(
    'badge values across empty and non-empty write-backs always mirror ' +
      'the record list exactly',
    () => {
      // Mixed generator: empty and non-empty lists in one property, so
      // the one-badge-per-entry law and the placeholder fallback are the
      // same invariant — badges === write-back list (Req 2.4, 2.8).
      const publishedMixedArchsArb = fc.oneof(
        publishedEmptyArchsArb,
        publishedNonEmptyArchsArb
      );
      fc.assert(
        fc.property(
          vllmRecordWith(publishedMixedArchsArb),
          roleArb,
          sessionArb,
          (record, role, session) => {
            const panel = derivePanelState(record, role, session);
            expect(panel.publishedSection!.supportedArchitectures).toEqual(
              record.published_component!.supported_architectures
            );
          }
        ),
        { numRuns: 100 }
      );
    }
  );
});
