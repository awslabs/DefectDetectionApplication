/**
 * Property-based test for the `WinnerPodium` component
 * (labeling-job-cleanup-work-stealing-and-podium task 2.9, design
 * Property 12).
 *
 * Feature: labeling-job-cleanup-work-stealing-and-podium, Property 12:
 * The Winner_Podium renders its entries faithfully and only when non-empty
 *
 * **Validates: Requirements 8.4, 8.5**
 *
 * The generator produces Podium_Entry lists of length 0 to 3 — the exact
 * shapes the backend's `podium_ranking` emits (places 1..N in rank order,
 * fewer entries when fewer submitters exist, an empty list when none do) —
 * with entries independently carrying or omitting an email, and varied
 * submitted counts, final-submission timestamps, and user ids. The
 * property asserts:
 *
 * - an empty list renders nothing at all: no `winner-podium` testid, no
 *   place columns, an empty container (Req 8.5);
 * - a non-empty list renders exactly one `podium-place-{p}` column per
 *   entry, each showing its textual place ("1st place" etc.), its display
 *   name (the email exactly when carried, the user id otherwise), and its
 *   submitted count text (Req 8.4); and
 * - the 1st-place column is visually most prominent: its pedestal height
 *   strictly exceeds the 2nd and 3rd pedestals whenever those places are
 *   present (Req 8.4).
 *
 * Scaffolding follows the shipped component-property precedent
 * `PreviewResultCanvas.score.property.test.tsx` (fast-check + vitest +
 * testing-library, `cleanup()` per run, `{ numRuns: 100 }`).
 */
import { describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import * as fc from 'fast-check';

import WinnerPodium from './WinnerPodium';
import type { PodiumEntry } from '../../services/api';

/** The textual place wording per rank (Req 8.4 — place shown as text). */
const PLACE_TEXT: Record<1 | 2 | 3, string> = {
  1: '1st place',
  2: '2nd place',
  3: '3rd place',
};

/* ------------------------------------------------------------------ */
/* Generators                                                          */
/* ------------------------------------------------------------------ */

/**
 * Raw per-entry material, mapped onto a `PodiumEntry` once its rank
 * position is known. User ids and emails come from disjoint textual
 * shapes (`user-sub-…` vs `labeler…@example.com`), so an email-carrying
 * column can be asserted to show the email and not the user id without
 * accidental-substring false positives.
 */
interface EntrySpec {
  idNum: number;
  /** Email suffix, or null to omit the email entirely (departed member). */
  emailNum: number | null;
  submitted: number;
  finalSubmittedAt: number;
}

const entrySpecArb: fc.Arbitrary<EntrySpec> = fc.record({
  idNum: fc.integer({ min: 0, max: 999_999 }),
  emailNum: fc.option(fc.integer({ min: 0, max: 999_999 }), { nil: null }),
  submitted: fc.integer({ min: 0, max: 9_999 }),
  // Epoch seconds across the plausible range of Final_Submission_Timestamps.
  finalSubmittedAt: fc.integer({ min: 0, max: 2_000_000_000 }),
});

/**
 * Podium_Entry lists of length 0-3 with places 1..N in rank order — the
 * Podium_Ranking output contract (Req 7.4) this component consumes.
 */
const entriesArb: fc.Arbitrary<PodiumEntry[]> = fc
  .array(entrySpecArb, { minLength: 0, maxLength: 3 })
  .map((specs) =>
    specs.map((spec, i) => {
      const entry: PodiumEntry = {
        place: (i + 1) as 1 | 2 | 3,
        // The place prefix keeps user ids distinct across one podium.
        user_id: `user-sub-${i + 1}-${spec.idNum}`,
        submitted: spec.submitted,
        final_submitted_at: spec.finalSubmittedAt,
      };
      if (spec.emailNum !== null) {
        entry.email = `labeler${spec.emailNum}@example.com`;
      }
      return entry;
    })
  );

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/** The one pedestal inside a place column: the div carrying a height style. */
function pedestalHeight(column: HTMLElement): number {
  const pedestal = Array.from(column.querySelectorAll('div')).find(
    (div) => div.style.height !== ''
  );
  expect(pedestal).toBeDefined();
  return Number.parseFloat((pedestal as HTMLDivElement).style.height);
}

/* ------------------------------------------------------------------ */
/* Property 12 (task 2.9)                                              */
/* ------------------------------------------------------------------ */

describe('Feature: labeling-job-cleanup-work-stealing-and-podium, Property 12: The Winner_Podium renders its entries faithfully and only when non-empty', () => {
  /**
   * *For any* Podium_Entry list of length 0 to 3 (entries with and without
   * emails), the component SHALL render nothing for an empty list, and
   * otherwise SHALL render one place marker per entry with the 1st-place
   * entry most prominent, each showing its place, its display name (email
   * when carried, user id otherwise), and its submitted count.
   *
   * **Validates: Requirements 8.4, 8.5**
   */
  it('renders nothing when empty, and one faithful place column per entry with 1st tallest otherwise', () => {
    fc.assert(
      fc.property(entriesArb, (entries) => {
        cleanup();
        const view = render(<WinnerPodium entries={entries} />);
        const { container } = view;

        if (entries.length === 0) {
          // An empty podium renders nothing at all (Req 8.5).
          expect(
            container.querySelectorAll('[data-testid="winner-podium"]')
          ).toHaveLength(0);
          expect(
            container.querySelectorAll('[data-testid^="podium-place-"]')
          ).toHaveLength(0);
          expect(container.childElementCount).toBe(0);
          view.unmount();
          return;
        }

        // One podium, and exactly one place column per entry — no extra
        // columns for places that have no submitter (Req 8.4).
        expect(
          container.querySelectorAll('[data-testid="winner-podium"]')
        ).toHaveLength(1);
        expect(
          container.querySelectorAll('[data-testid^="podium-place-"]')
        ).toHaveLength(entries.length);

        for (const entry of entries) {
          const columns = container.querySelectorAll(
            `[data-testid="podium-place-${entry.place}"]`
          );
          expect(columns).toHaveLength(1);
          const column = columns[0] as HTMLElement;
          const text = column.textContent ?? '';

          // The place is shown as text, not just visual height (Req 8.4).
          expect(text).toContain(PLACE_TEXT[entry.place]);

          // Display name: the email exactly when carried, the user id
          // otherwise (Req 8.4). The disjoint generator shapes make the
          // not-the-user-id assertion substring-safe.
          const displayName = entry.email ?? entry.user_id;
          expect(text).toContain(displayName);
          if (entry.email !== undefined) {
            expect(text).not.toContain(entry.user_id);
          }

          // The entry's Submitted count as text (Req 8.4).
          expect(text).toContain(`${entry.submitted} submitted`);
        }

        // 1st place is visually most prominent: its pedestal is strictly
        // taller than 2nd's and 3rd's whenever they exist (Req 8.4).
        const firstColumn = container.querySelector(
          '[data-testid="podium-place-1"]'
        ) as HTMLElement;
        const firstHeight = pedestalHeight(firstColumn);
        for (const place of [2, 3] as const) {
          const column = container.querySelector(
            `[data-testid="podium-place-${place}"]`
          );
          if (column !== null) {
            expect(firstHeight).toBeGreaterThan(
              pedestalHeight(column as HTMLElement)
            );
          }
        }

        view.unmount();
      }),
      { numRuns: 100 }
    );
  }, 600_000);
});
