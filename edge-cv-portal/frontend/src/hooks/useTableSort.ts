import { useMemo, useState } from 'react';
import { TableProps } from '@cloudscape-design/components';

/**
 * useTableSort
 *
 * Cloudscape's <Table> renders sort arrows whenever a column defines a
 * `sortingField` (or `sortingComparator`), but clicking them does nothing
 * unless the table is also given `sortingColumn`, `sortingDescending`, and
 * `onSortingChange`, AND the items array is actually re-sorted.
 *
 * This hook centralizes that wiring. Pass in the raw items and get back the
 * sorted items plus the props to spread onto the <Table>.
 *
 * Usage:
 *   const { items: sortedItems, sortingProps } = useTableSort(rawItems);
 *   <Table items={sortedItems} {...sortingProps} columnDefinitions={[...]} />
 */
export function useTableSort<T>(
  items: T[],
  defaults?: {
    sortingColumn?: TableProps.SortingColumn<T>;
    sortingDescending?: boolean;
  }
) {
  const [sortingColumn, setSortingColumn] = useState<
    TableProps.SortingColumn<T> | undefined
  >(defaults?.sortingColumn);
  const [sortingDescending, setSortingDescending] = useState<boolean>(
    defaults?.sortingDescending ?? false
  );

  const sortedItems = useMemo(() => {
    if (!sortingColumn) return items;

    const { sortingComparator, sortingField } = sortingColumn;

    const comparator =
      sortingComparator ||
      ((a: T, b: T) => {
        if (!sortingField) return 0;
        const av = (a as Record<string, unknown>)[sortingField as string];
        const bv = (b as Record<string, unknown>)[sortingField as string];

        if (av == null && bv == null) return 0;
        if (av == null) return -1;
        if (bv == null) return 1;

        // Numeric comparison when both are numbers
        if (typeof av === 'number' && typeof bv === 'number') {
          return av - bv;
        }

        // Date-like strings / values: try Date parse first
        const ad = Date.parse(String(av));
        const bd = Date.parse(String(bv));
        if (!Number.isNaN(ad) && !Number.isNaN(bd)) {
          return ad - bd;
        }

        // Fallback to locale-aware string comparison
        return String(av).localeCompare(String(bv), undefined, {
          numeric: true,
          sensitivity: 'base',
        });
      });

    const sorted = [...items].sort(comparator);
    return sortingDescending ? sorted.reverse() : sorted;
  }, [items, sortingColumn, sortingDescending]);

  const sortingProps: Pick<
    TableProps<T>,
    'sortingColumn' | 'sortingDescending' | 'onSortingChange'
  > = {
    sortingColumn,
    sortingDescending,
    onSortingChange: ({ detail }) => {
      setSortingColumn(detail.sortingColumn);
      setSortingDescending(detail.isDescending ?? false);
    },
  };

  return { items: sortedItems, sortingProps };
}
