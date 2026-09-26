/**
 * The one compilation-target list (utils/compilationTargets.ts), shared by the
 * Compilation tab and the Smart Import / Model Import auto-compile pickers.
 */
import { describe, expect, it } from 'vitest';

import { COMPILATION_TARGETS, COMPILATION_TARGET_OPTIONS } from './compilationTargets';

// compilation.COMPILATION_TARGETS keys, in the order the backend lists them in
// its "Invalid targets ... Valid targets: ..." 400.
const BACKEND_TARGETS = ['jetson-xavier-jp5', 'jetson-xavier-jp6', 'x86_64-cpu', 'x86_64-cuda', 'arm64-cpu', 'onnx'];

describe('compilation targets', () => {
  it('offers exactly what the compile endpoint accepts, ONNX included, JetPack 4 retired', () => {
    expect(COMPILATION_TARGETS.map(t => t.id)).toEqual(BACKEND_TARGETS);
    expect(COMPILATION_TARGETS.map(t => t.id)).not.toContain('jetson-xavier');
  });

  it('gives the import pages the same targets, names and descriptions as Multiselect options', () => {
    expect(COMPILATION_TARGET_OPTIONS).toEqual(
      COMPILATION_TARGETS.map(t => ({ label: t.name, value: t.id, description: t.description }))
    );
    expect(COMPILATION_TARGET_OPTIONS.find(o => o.value === 'onnx')?.label).toBe('ONNX Runtime (portable)');
  });
});
