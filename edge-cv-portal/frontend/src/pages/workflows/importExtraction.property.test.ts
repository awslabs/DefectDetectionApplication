/**
 * **Feature: custom-node-code-assist, Property 4: Import extraction completeness**
 *
 * For all Python module codes built by planting known import statements
 * (plain, aliased, multi-name, `from … import`, dotted, top-level or nested
 * in function bodies and conditional blocks) among filler statements,
 * comments, and import-mentioning string literals, `extractImports` returns
 * `ok: true` with exactly the planted absolute top-level module names
 * (deduped, in first-occurrence order). Relative imports are planted too and
 * contribute no names.
 *
 * **Validates: Requirements 3.1**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { extractImports } from './importAnalyzer';

// --------------------------------------------------------------------------
// Identifier and module-path generators
// --------------------------------------------------------------------------

/**
 * Top-level module names drawn from a small pool (so dedup across planted
 * imports is exercised) plus generated `mod_{n}` identifiers.
 */
const identArb: fc.Arbitrary<string> = fc.oneof(
  fc.constantFrom('alpha', 'beta', 'gamma', 'numpy', 'cv2', 'pkg', 'data', '_private', 'x1'),
  fc.nat({ max: 20 }).map((n) => `mod_${n}`)
);

/** Dotted module path `a.b.c` (1–3 segments). */
const dottedArb: fc.Arbitrary<string> = fc
  .array(identArb, { minLength: 1, maxLength: 3 })
  .map((parts) => parts.join('.'));

/** `a.b.c` → `a`. */
function topLevel(dotted: string): string {
  return dotted.split('.')[0];
}

/** Names bound by `from … import` (irrelevant to the extracted set). */
const boundNameArb: fc.Arbitrary<string> = fc.constantFrom('x', 'y', 'z', 'item', 'val');

/** Aliases for `as` clauses (irrelevant to the extracted set). */
const aliasArb: fc.Arbitrary<string> = fc.constantFrom('a1', 'a2', 'renamed');

/** Rendered `x` / `x as a1` name list for `from … import`. */
const nameListArb: fc.Arbitrary<string[]> = fc
  .array(
    fc.record({ name: boundNameArb, alias: fc.option(aliasArb, { nil: undefined }) }),
    { minLength: 1, maxLength: 3 }
  )
  .map((entries) =>
    entries.map(({ name, alias }) => (alias === undefined ? name : `${name} as ${alias}`))
  );

// --------------------------------------------------------------------------
// Planted import statements
// --------------------------------------------------------------------------

/** One planted import: its source text and the absolute top-level names it contributes. */
interface PlantedImport {
  text: string;
  names: string[];
}

/**
 * `import a.b.c as x, d` — plain, aliased, multi-name, optionally split
 * across lines with a backslash continuation.
 */
const importStmtArb: fc.Arbitrary<PlantedImport> = fc
  .record({
    items: fc.array(
      fc.record({ module: dottedArb, alias: fc.option(aliasArb, { nil: undefined }) }),
      { minLength: 1, maxLength: 3 }
    ),
    backslash: fc.boolean(),
  })
  .map(({ items, backslash }) => {
    const rendered = items.map(({ module, alias }) =>
      alias === undefined ? module : `${module} as ${alias}`
    );
    const joiner = backslash && items.length > 1 ? ', \\\n    ' : ', ';
    return {
      text: `import ${rendered.join(joiner)}`,
      names: items.map(({ module }) => topLevel(module)),
    };
  });

/**
 * `from a.b import …` — star, bare name list, or parenthesized name list
 * (optionally multiline with a trailing comma). Contributes the module's
 * top-level name.
 */
const fromImportArb: fc.Arbitrary<PlantedImport> = fc.oneof(
  dottedArb.map((module) => ({
    text: `from ${module} import *`,
    names: [topLevel(module)],
  })),
  fc.record({ module: dottedArb, names: nameListArb }).map(({ module, names }) => ({
    text: `from ${module} import ${names.join(', ')}`,
    names: [topLevel(module)],
  })),
  fc
    .record({
      module: dottedArb,
      names: nameListArb,
      multiline: fc.boolean(),
      trailingComma: fc.boolean(),
    })
    .map(({ module, names, multiline, trailingComma }) => {
      const comma = trailingComma ? ',' : '';
      const inner = multiline
        ? `\n    ${names.join(',\n    ')}${comma}\n`
        : `${names.join(', ')}${comma}`;
      return { text: `from ${module} import (${inner})`, names: [topLevel(module)] };
    })
);

/** `from . import x` / `from ..pkg import y` — recognized but contributes nothing. */
const relativeImportArb: fc.Arbitrary<PlantedImport> = fc
  .record({
    dots: fc.integer({ min: 1, max: 2 }).map((n) => '.'.repeat(n)),
    module: fc.option(dottedArb, { nil: undefined }),
    names: nameListArb,
  })
  .map(({ dots, module, names }) => ({
    text: `from ${dots}${module ?? ''} import ${names.join(', ')}`,
    names: [],
  }));

// --------------------------------------------------------------------------
// Placement (top-level or nested) and filler statements
// --------------------------------------------------------------------------

type Placement = 'top' | 'function' | 'conditional';

const placementArb: fc.Arbitrary<Placement> = fc.constantFrom('top', 'function', 'conditional');

/** Indent every physical line of a (possibly multiline) statement. */
function indent(text: string): string {
  return text
    .split('\n')
    .map((line) => `    ${line}`)
    .join('\n');
}

/** Render a planted import at its placement. */
function place(stmt: PlantedImport, placement: Placement, i: number): string {
  switch (placement) {
    case 'top':
      return stmt.text;
    case 'function':
      return `def fn_${i}():\n${indent(stmt.text)}\n    return None`;
    case 'conditional':
      return `if True:\n${indent(stmt.text)}`;
  }
}

/**
 * Filler that must never contribute an import: plain statements, comments,
 * and string literals that mention imports.
 */
const fillerArb: fc.Arbitrary<string> = fc.constantFrom(
  '# import commented_out',
  'value = 1 + 2',
  "note = 'import fake_single'",
  'text = "from fake_double import thing"',
  'doc = """\nimport fake_triple\nfrom fake_triple import x\n"""',
  'def helper():\n    return 42',
  'items = [1,\n         2]',
  'print(value)',
  ''
);

type Block =
  | { kind: 'planted'; stmt: PlantedImport; placement: Placement }
  | { kind: 'filler'; text: string };

const blockArb: fc.Arbitrary<Block> = fc.oneof(
  fc.record({
    kind: fc.constant<'planted'>('planted'),
    stmt: fc.oneof(importStmtArb, fromImportArb, relativeImportArb),
    placement: placementArb,
  }),
  fillerArb.map((text): Block => ({ kind: 'filler', text }))
);

const moduleBlocksArb: fc.Arbitrary<Block[]> = fc.array(blockArb, {
  minLength: 0,
  maxLength: 12,
});

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 4: Import extraction completeness', () => {
  it('extracts exactly the planted absolute top-level names', () => {
    fc.assert(
      fc.property(moduleBlocksArb, (blocks) => {
        const lines: string[] = [];
        const expected: string[] = [];
        const seen = new Set<string>();

        blocks.forEach((block, i) => {
          if (block.kind === 'filler') {
            lines.push(block.text);
            return;
          }
          lines.push(place(block.stmt, block.placement, i));
          for (const name of block.stmt.names) {
            if (!seen.has(name)) {
              seen.add(name);
              expected.push(name);
            }
          }
        });

        const result = extractImports(lines.join('\n'));
        expect(result).toEqual({ ok: true, imports: expected });
      }),
      { numRuns: 100 }
    );
  });
});
