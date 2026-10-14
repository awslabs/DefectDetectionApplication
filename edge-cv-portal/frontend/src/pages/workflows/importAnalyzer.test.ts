import { describe, it, expect } from 'vitest';
import {
  DERIVED_MARKER,
  deriveRequirements,
  extractImports,
  parseRequirements,
  reconcileRequirements,
  renderRequirements,
} from './importAnalyzer';

/**
 * Unit tests for the Import_Analyzer's `extractImports` scanner
 * (Requirements 3.1, 3.3, 3.10). Property tests live in the dedicated
 * property-test tasks; these are example-based checks of the grammar,
 * stripping, and continuation handling.
 */

describe('extractImports', () => {
  it('extracts plain, aliased, and multi-name import statements', () => {
    expect(extractImports('import os')).toEqual({ ok: true, imports: ['os'] });
    expect(extractImports('import a.b.c as x, d')).toEqual({
      ok: true,
      imports: ['a', 'd'],
    });
  });

  it('extracts the top-level package from from-imports', () => {
    expect(extractImports('from a.b import x, y')).toEqual({
      ok: true,
      imports: ['a'],
    });
    expect(extractImports('from cv2 import aruco as ar')).toEqual({
      ok: true,
      imports: ['cv2'],
    });
    expect(extractImports('from numpy import *')).toEqual({
      ok: true,
      imports: ['numpy'],
    });
  });

  it('excludes relative imports but treats them as valid', () => {
    expect(extractImports('from . import helpers')).toEqual({
      ok: true,
      imports: [],
    });
    expect(extractImports('from .sibling import thing')).toEqual({
      ok: true,
      imports: [],
    });
  });

  it('finds imports nested at any indentation', () => {
    const code = [
      'def process_frame(frame, metadata):',
      '    import cv2',
      '    if metadata.get("gray"):',
      '        from PIL import Image',
      '    return frame',
    ].join('\n');
    expect(extractImports(code)).toEqual({ ok: true, imports: ['cv2', 'PIL'] });
  });

  it('dedupes repeated top-level names', () => {
    const code = 'import numpy\nfrom numpy import linalg\nimport numpy as np';
    expect(extractImports(code)).toEqual({ ok: true, imports: ['numpy'] });
  });

  it('ignores imports inside comments and string literals', () => {
    const code = [
      '# import fake_module',
      'x = "import another_fake"',
      "doc = '''",
      'import triple_quoted_fake',
      "'''",
      'import real_module',
    ].join('\n');
    expect(extractImports(code)).toEqual({ ok: true, imports: ['real_module'] });
  });

  it('joins backslash and parenthesized continuations', () => {
    const backslash = 'import os, \\\n    sys';
    expect(extractImports(backslash)).toEqual({ ok: true, imports: ['os', 'sys'] });
    const parens = 'from a.b import (\n    x,\n    y,\n)';
    expect(extractImports(parens)).toEqual({ ok: true, imports: ['a'] });
  });

  it('returns ok:false for an unterminated string literal', () => {
    expect(extractImports('x = "unterminated\nimport os')).toEqual({ ok: false });
    expect(extractImports("s = '''never closed\nimport os")).toEqual({ ok: false });
  });

  it('returns ok:false for a malformed import statement', () => {
    expect(extractImports('import')).toEqual({ ok: false });
    expect(extractImports('from a')).toEqual({ ok: false });
    expect(extractImports('from import x')).toEqual({ ok: false });
    expect(extractImports('import 123bad')).toEqual({ ok: false });
  });

  it('returns ok:false for an unterminated bracket that could swallow imports', () => {
    expect(extractImports('x = foo(\nimport os')).toEqual({ ok: false });
  });

  it('leaves non-import syntax errors alone and still extracts imports', () => {
    const code = 'import requests\ndef broken frame:\n    return @@';
    expect(extractImports(code)).toEqual({ ok: true, imports: ['requests'] });
  });

  it('handles empty and whitespace-only code', () => {
    expect(extractImports('')).toEqual({ ok: true, imports: [] });
    expect(extractImports('   \n\n  ')).toEqual({ ok: true, imports: [] });
  });
});

describe('deriveRequirements', () => {
  it('maps import names through the Import_Mapping with needsReview: false', () => {
    expect(deriveRequirements(['cv2'])).toEqual([
      { distribution: 'opencv-python-headless', needsReview: false },
    ]);
    expect(deriveRequirements(['PIL', 'sklearn', 'yaml'])).toEqual([
      { distribution: 'Pillow', needsReview: false },
      { distribution: 'PyYAML', needsReview: false },
      { distribution: 'scikit-learn', needsReview: false },
    ]);
  });

  it('maps identity entries like numpy with needsReview: false', () => {
    expect(deriveRequirements(['numpy'])).toEqual([
      { distribution: 'numpy', needsReview: false },
    ]);
  });

  it('drops standard-library modules and dda_frames', () => {
    expect(deriveRequirements(['os', 'sys', 'json', '__future__', 'dda_frames'])).toEqual([]);
    // 3.9-only and 3.11-only stdlib names are both excluded.
    expect(deriveRequirements(['binhex', 'tomllib'])).toEqual([]);
  });

  it('falls through to identity plus needsReview: true for unmapped names', () => {
    expect(deriveRequirements(['some_unknown_lib'])).toEqual([
      { distribution: 'some_unknown_lib', needsReview: true },
    ]);
  });

  it('sorts output by distribution and dedupes repeated names', () => {
    expect(deriveRequirements(['requests', 'cv2', 'requests', 'cv2'])).toEqual([
      { distribution: 'opencv-python-headless', needsReview: false },
      { distribution: 'requests', needsReview: false },
    ]);
  });

  it('handles a mixed realistic import set', () => {
    const imports = ['cv2', 'numpy', 'os', 'dda_frames', 'mystery_pkg'];
    expect(deriveRequirements(imports)).toEqual([
      { distribution: 'mystery_pkg', needsReview: true },
      { distribution: 'numpy', needsReview: false },
      { distribution: 'opencv-python-headless', needsReview: false },
    ]);
  });

  it('returns an empty list for no imports', () => {
    expect(deriveRequirements([])).toEqual([]);
  });
});

/**
 * Unit tests for requirements parsing, rendering, and reconciliation
 * (Requirements 3.5, 3.9). Example-based; the corresponding property
 * tests are separate tasks (Properties 6-8).
 */

describe('parseRequirements / renderRequirements', () => {
  it('round-trips a mixed requirements text exactly', () => {
    const text = [
      '# my pins',
      'numpy==1.24.0',
      '',
      'requests>=2.0  # keep this',
      `opencv-python-headless  ${DERIVED_MARKER}`,
      `mystery_pkg  ${DERIVED_MARKER} (verify package name)`,
    ].join('\n');
    expect(renderRequirements(parseRequirements(text))).toBe(text);
  });

  it('parses an empty text to no entries', () => {
    expect(parseRequirements('')).toEqual([]);
    expect(renderRequirements([])).toBe('');
  });

  it('flags derived and needs-review lines and normalizes distributions', () => {
    const entries = parseRequirements(
      [
        'Numpy==1.24.0',
        'My_Package.Name>=1.0',
        `Pillow  ${DERIVED_MARKER}`,
        `some_lib  ${DERIVED_MARKER} (verify package name)`,
        '# just a comment',
        '',
      ].join('\n')
    );
    expect(entries.map((e) => e.distribution)).toEqual([
      'numpy',
      'my-package-name',
      'pillow',
      'some-lib',
      null,
      null,
    ]);
    expect(entries.map((e) => e.derived)).toEqual([false, false, true, true, false, false]);
    expect(entries.map((e) => e.needsReview)).toEqual([
      false,
      false,
      false,
      true,
      false,
      false,
    ]);
  });
});

describe('reconcileRequirements', () => {
  it('appends marker lines for derived entries into an empty text', () => {
    const result = reconcileRequirements('', [
      { distribution: 'opencv-python-headless', needsReview: false },
      { distribution: 'mystery_pkg', needsReview: true },
    ]);
    expect(result).toBe(
      [
        `opencv-python-headless  ${DERIVED_MARKER}`,
        `mystery_pkg  ${DERIVED_MARKER} (verify package name)`,
      ].join('\n')
    );
  });

  it('keeps manual lines verbatim and in order', () => {
    const current = ['# pinned by me', 'numpy==1.24.0', '', 'requests>=2.0  # keep'].join('\n');
    const result = reconcileRequirements(current, [
      { distribution: 'Pillow', needsReview: false },
    ]);
    expect(result).toBe(`${current}\nPillow  ${DERIVED_MARKER}`);
  });

  it('drops previously derived lines that are no longer derived', () => {
    const current = [
      'numpy==1.24.0',
      `opencv-python-headless  ${DERIVED_MARKER}`,
      `old_pkg  ${DERIVED_MARKER} (verify package name)`,
    ].join('\n');
    const result = reconcileRequirements(current, [
      { distribution: 'opencv-python-headless', needsReview: false },
    ]);
    expect(result).toBe(
      ['numpy==1.24.0', `opencv-python-headless  ${DERIVED_MARKER}`].join('\n')
    );
  });

  it('does not duplicate a manually pinned distribution (PEP 503 match)', () => {
    // Manual pin `Numpy==1.24.0` normalizes to `numpy`, matching the
    // derived `numpy` entry, so no derived line is added for it.
    const current = 'Numpy==1.24.0';
    const result = reconcileRequirements(current, [
      { distribution: 'numpy', needsReview: false },
      { distribution: 'requests', needsReview: false },
    ]);
    expect(result).toBe(['Numpy==1.24.0', `requests  ${DERIVED_MARKER}`].join('\n'));
  });

  it('matches manual entries across -/_/. name variants', () => {
    const current = 'scikit_learn==1.3.0';
    const result = reconcileRequirements(current, [
      { distribution: 'scikit-learn', needsReview: false },
    ]);
    expect(result).toBe('scikit_learn==1.3.0');
  });

  it('is idempotent for a fixed derived list', () => {
    const current = [
      '# tools',
      'numpy==1.24.0',
      `stale_pkg  ${DERIVED_MARKER}`,
      '',
      'requests',
    ].join('\n');
    const derived = [
      { distribution: 'opencv-python-headless', needsReview: false },
      { distribution: 'mystery_pkg', needsReview: true },
      { distribution: 'requests', needsReview: false },
    ];
    const once = reconcileRequirements(current, derived);
    const twice = reconcileRequirements(once, derived);
    expect(twice).toBe(once);
  });

  it('clears derived lines when the derived list is empty', () => {
    const current = ['numpy==1.24.0', `Pillow  ${DERIVED_MARKER}`].join('\n');
    expect(reconcileRequirements(current, [])).toBe('numpy==1.24.0');
  });
});
