// Vitest global setup for component tests.
//
// Registers the custom `@testing-library/jest-dom` matchers (e.g.
// `toBeInTheDocument`, `toBeDisabled`) and cleans up the rendered DOM after
// each test so tests stay isolated.
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => {
  cleanup();
});
