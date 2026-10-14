/**
 * **Feature: custom-node-code-assist, Property 9: Unparseable code changes nothing**
 *
 * For all module codes corrupted by injecting a malformed import statement
 * (an `import`/`from`-leading line that does not match the import grammar)
 * or an unterminated string literal, `extractImports` returns `ok: false`,
 * and the surface's derivation step — which applies nothing when the scan
 * fails — leaves the current requirements text byte-identical.
 *
 * **Validates: Requirements 3.10**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { extractImports, deriveRequirements, reconcileRequirements } from './importAnalyzer';

// --------------------------------------------------------------------------
// Base module builder (quote-free, one logical line per statement, so an
// injected corruption stays its own logical line and an injected
// unterminated string can never be closed by later source text)
// --------------------------------------------------------------------------

const identArb: fc.Arbitrary<string> = fc.oneof(
  fc.constantFrom('numpy', 'cv2', 'pkg', 'alpha', '_private'),
  fc.nat({ max: 20 }).map((n) => `mod_${n}`)
);

const baseStatementArb: fc.Arbitrary<string> = fc.oneof(
  fc.constantFrom(
    'import numpy',
    'import cv2 as cv',
    'from pandas import DataFrame',
    'from . import sibling',
    'value = 1 + 2',
    '# a comment mentioning import fake',
    'def helper():\n    return 42',
    'print(value)',
    ''
  ),
  identArb.map((name) => `import ${name}`),
  identArb.map((name) => `from ${name} import thing`)
);

// --------------------------------------------------------------------------
// Corruption arbitrary: malformed import statement or unterminated string
// --------------------------------------------------------------------------

/** `import`/`from`-leading lines that do not match the import grammar. */
const malformedImportArb: fc.Arbitrary<string> = fc.constantFrom(
  'import',
  'import 123abc',
  'import a-b',
  'import numpy,',
  'import .relative',
  'from import x',
  'from a import',
  'from a b import x',
  'from a import x y'
);

/**
 * A line opening a string literal that is never terminated. Bodies are
 * free of quotes, backslashes, and newlines; single-quoted strings die at
 * the injected line's end and triple-quoted ones run to EOF unclosed
 * (the quote-free base module cannot terminate them).
 */
const unterminatedStringArb: fc.Arbitrary<string> = fc
  .record({
    quote: fc.constantFrom('"', "'", '"""', "'''"),
    body: fc.constantFrom('', 'abc', 'never closed', 'text with spaces'),
  })
  .map(({ quote, body }) => `s = ${quote}${body}`);

const corruptionArb: fc.Arbitrary<string> = fc.oneof(malformedImportArb, unterminatedStringArb);

// --------------------------------------------------------------------------
// Current requirements text (manual lines, pins, comments, marker lines)
// --------------------------------------------------------------------------

const requirementsLineArb: fc.Arbitrary<string> = fc.oneof(
  fc.constantFrom(
    'numpy==1.24.0',
    'requests',
    '# manual comment',
    '',
    'opencv-python-headless  # via code imports',
    'somepkg  # via code imports (verify package name)'
  ),
  fc.string({ maxLength: 30 })
);

const requirementsTextArb: fc.Arbitrary<string> = fc
  .array(requirementsLineArb, { minLength: 0, maxLength: 8 })
  .map((lines) => lines.join('\n'));

// --------------------------------------------------------------------------
// The surface's derivation step: an {ok:false} scan applies nothing
// --------------------------------------------------------------------------

function surfaceDerivationStep(currentText: string, code: string): string {
  const scan = extractImports(code);
  if (!scan.ok) {
    return currentText;
  }
  return reconcileRequirements(currentText, deriveRequirements(scan.imports));
}

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 9: Unparseable code changes nothing', () => {
  it('scans corrupted code as ok:false and leaves the requirements text byte-identical', () => {
    fc.assert(
      fc.property(
        fc.array(baseStatementArb, { minLength: 0, maxLength: 10 }),
        corruptionArb,
        fc.nat({ max: 100 }),
        requirementsTextArb,
        (statements, corruption, positionSeed, requirementsText) => {
          // Inject the corruption at a line boundary somewhere in the module.
          const position = positionSeed % (statements.length + 1);
          const lines = [...statements];
          lines.splice(position, 0, corruption);
          const code = lines.join('\n');

          const scan = extractImports(code);
          expect(scan.ok).toBe(false);

          const after = surfaceDerivationStep(requirementsText, code);
          expect(after).toBe(requirementsText);
        }
      ),
      { numRuns: 100 }
    );
  });
});
