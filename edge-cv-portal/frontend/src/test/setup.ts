// Vitest global setup for component tests.
//
// Registers the custom `@testing-library/jest-dom` matchers (e.g.
// `toBeInTheDocument`, `toBeDisabled`) and cleans up the rendered DOM after
// each test so tests stay isolated.
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import fc from 'fast-check';

// Cap fast-check property tests at 25 runs for fast local suites.
// Explicit `numRuns` passed to fc.assert still takes precedence; keep those
// at or below this budget.
fc.configureGlobal({ numRuns: 25 });

afterEach(() => {
  cleanup();
});
