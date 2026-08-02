/**
 * Tests for the Deployments list default sort (device-arch-compatibility
 * Workstream C, design C1). The list defaults to newest-first by
 * `creation_timestamp`, ties break deterministically on `deployment_id`, and
 * deployments with no resolvable `creation_timestamp` sort last without being
 * dropped.
 *
 * The sort behavior lives in `useTableSort` driven by the exported
 * `createdSortingComparator` + `deploymentsSortingDefaults`, so these tests
 * exercise that exact wiring rather than re-rendering the whole page.
 */
import { describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import fc from 'fast-check';

import { useTableSort } from '../hooks/useTableSort';
import {
  createdSortingComparator,
  deploymentsSortingDefaults,
} from './Deployments';

interface Row {
  deployment_id: string;
  creation_timestamp: string | null;
}

function sortWithDefault(rows: Row[]): Row[] {
  const { result } = renderHook(() =>
    useTableSort(rows, deploymentsSortingDefaults)
  );
  return result.current.items;
}

describe('Deployments list default sort', () => {
  it('renders newest-first by default (Req 6.1)', () => {
    const rows: Row[] = [
      { deployment_id: 'b', creation_timestamp: '2024-01-02T00:00:00Z' },
      { deployment_id: 'a', creation_timestamp: '2024-01-01T00:00:00Z' },
      { deployment_id: 'c', creation_timestamp: '2024-01-03T00:00:00Z' },
    ];

    const items = sortWithDefault(rows);

    expect(items.map((r) => r.deployment_id)).toEqual(['c', 'b', 'a']);
  });

  it('keeps a deterministic order for equal timestamps (Req 6.2)', () => {
    const ts = '2024-05-01T12:00:00Z';
    const rows: Row[] = [
      { deployment_id: 'd-2', creation_timestamp: ts },
      { deployment_id: 'd-1', creation_timestamp: ts },
      { deployment_id: 'd-3', creation_timestamp: ts },
    ];

    const first = sortWithDefault(rows).map((r) => r.deployment_id);
    // Same input in a different arrival order yields the same output order.
    const shuffled = [rows[2], rows[0], rows[1]];
    const second = sortWithDefault(shuffled).map((r) => r.deployment_id);

    expect(first).toEqual(second);
    // Ascending tie-break on deployment_id, reversed by the descending default.
    expect(first).toEqual(['d-3', 'd-2', 'd-1']);
  });

  it('sorts a row with a null/absent creation_timestamp last without dropping it (Req 6.3)', () => {
    const rows: Row[] = [
      { deployment_id: 'has-date', creation_timestamp: '2024-01-02T00:00:00Z' },
      { deployment_id: 'no-date', creation_timestamp: null },
      { deployment_id: 'older', creation_timestamp: '2024-01-01T00:00:00Z' },
      { deployment_id: 'bad-date', creation_timestamp: 'not-a-real-date' },
    ];

    const items = sortWithDefault(rows);

    expect(items).toHaveLength(rows.length);
    const ids = items.map((r) => r.deployment_id);
    // Parseable dates first (newest-first), unparseable/absent trailing.
    expect(ids.slice(0, 2)).toEqual(['has-date', 'older']);
    expect(ids.slice(2)).toContain('no-date');
    expect(ids.slice(2)).toContain('bad-date');
  });

  it('lets a user header click override the default (Req 6.4)', () => {
    const rows: Row[] = [
      { deployment_id: 'b', creation_timestamp: '2024-01-02T00:00:00Z' },
      { deployment_id: 'a', creation_timestamp: '2024-01-01T00:00:00Z' },
    ];

    const { result } = renderHook(() =>
      useTableSort(rows, deploymentsSortingDefaults)
    );
    // Default is newest-first.
    expect(result.current.items.map((r) => r.deployment_id)).toEqual([
      'b',
      'a',
    ]);

    // User clicks the "Created" header to sort ascending.
    act(() => {
      result.current.sortingProps.onSortingChange!({
        detail: {
          sortingColumn: deploymentsSortingDefaults.sortingColumn,
          isDescending: false,
        },
      } as never);
    });

    expect(result.current.items.map((r) => r.deployment_id)).toEqual([
      'a',
      'b',
    ]);
  });

  // Feature: device-arch-compatibility, Property 9: Sort placement of missing dates
  it('places every unparseable/absent timestamp after every parseable one and never drops an item', () => {
    const rowArb = fc.record({
      deployment_id: fc.string({ minLength: 1, maxLength: 8 }),
      creation_timestamp: fc.oneof(
        fc
          .date({
            min: new Date('2000-01-01T00:00:00Z'),
            max: new Date('2035-01-01T00:00:00Z'),
          })
          .map((d) => d.toISOString()),
        fc.constant<string | null>(null),
        fc.constant<string | null>(''),
        fc.constant<string | null>('not-a-date')
      ),
    });

    fc.assert(
      fc.property(
        // Unique deployment_id keeps the tie-break total; matches real data
        // where deployment_id is a key.
        fc.uniqueArray(rowArb, {
          selector: (r) => r.deployment_id,
          maxLength: 30,
        }),
        (rows) => {
          const items = sortWithDefault(rows);

          // Never drops an item.
          expect(items).toHaveLength(rows.length);
          expect([...items].sort((x, y) =>
            x.deployment_id.localeCompare(y.deployment_id)
          )).toEqual(
            [...rows].sort((x, y) =>
              x.deployment_id.localeCompare(y.deployment_id)
            )
          );

          const parseable = (r: Row) => {
            if (r.creation_timestamp == null) return false;
            return !Number.isNaN(Date.parse(String(r.creation_timestamp)));
          };

          // Once a missing/unparseable date appears, no parseable date follows.
          let seenMissing = false;
          for (const item of items) {
            if (parseable(item)) {
              expect(seenMissing).toBe(false);
            } else {
              seenMissing = true;
            }
          }
        }
      )
    );
  });
});

describe('createdSortingComparator', () => {
  it('orders parseable timestamps ascending with a deployment_id tie-break', () => {
    expect(
      createdSortingComparator(
        { deployment_id: 'a', creation_timestamp: '2024-01-01T00:00:00Z' },
        { deployment_id: 'b', creation_timestamp: '2024-01-02T00:00:00Z' }
      )
    ).toBeLessThan(0);

    expect(
      createdSortingComparator(
        { deployment_id: 'a', creation_timestamp: '2024-01-01T00:00:00Z' },
        { deployment_id: 'b', creation_timestamp: '2024-01-01T00:00:00Z' }
      )
    ).toBeLessThan(0);
  });

  it('orders missing dates before parseable ones (so they land last after reverse)', () => {
    expect(
      createdSortingComparator(
        { deployment_id: 'x', creation_timestamp: null },
        { deployment_id: 'y', creation_timestamp: '2024-01-01T00:00:00Z' }
      )
    ).toBe(-1);
    expect(
      createdSortingComparator(
        { deployment_id: 'x', creation_timestamp: '2024-01-01T00:00:00Z' },
        { deployment_id: 'y', creation_timestamp: null }
      )
    ).toBe(1);
  });
});
