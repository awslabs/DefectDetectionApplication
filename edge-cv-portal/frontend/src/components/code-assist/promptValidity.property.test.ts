/**
 * **Feature: custom-node-code-assist, Property 1: Prompt validity predicate**
 *
 * For any string, `isSubmittablePrompt` accepts it if and only if it contains
 * at least one non-whitespace character and its length is at most 4,000; a
 * rejected prompt never triggers a Code_Assist_Generator invocation — the
 * reducer never leaves `idle` on `submit` with a rejected prompt.
 *
 * **Validates: Requirements 1.4, 2.8**
 *
 * The oracle mirrors the module's definition (trimmed length ≥ 1 and total
 * length ≤ 4,000) but establishes the non-whitespace requirement per
 * character, so the two computations agree only when the predicate treats
 * every character consistently with `String.prototype.trim`. Lengths are
 * UTF-16 code units, matching `prompt.length`.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  codeAssistReducer,
  isSubmittablePrompt,
  PROMPT_MAX_LENGTH,
  type CodeAssistState,
} from './codeAssistState';

/** Characters `String.prototype.trim` removes (WhiteSpace ∪ LineTerminator). */
const whitespaceCharArb = fc.constantFrom(
  ' ',
  '\t',
  '\n',
  '\r',
  '\f',
  '\v',
  '\u00a0', // no-break space
  '\u1680', // ogham space mark
  '\u2000', // en quad
  '\u2007', // figure space
  '\u2028', // line separator
  '\u2029', // paragraph separator
  '\u202f', // narrow no-break space
  '\u3000', // ideographic space
  '\ufeff' // BOM / zero-width no-break space
);

/**
 * Single-code-unit non-whitespace characters, including unicode letters and
 * `\u200b` (zero-width space), which JS `trim` does NOT remove.
 */
const nonWhitespaceCharArb = fc.constantFrom(
  ...'abcXYZ019._-#{}"',
  'é',
  'ß',
  'π',
  '日',
  '本',
  '中',
  '\u200b'
);

const charArb = fc.oneof(whitespaceCharArb, nonWhitespaceCharArb);

/** Short-to-medium mixed unicode strings. */
const mixedPromptArb = fc.string({ unit: charArb, maxLength: 60 });

/** Whitespace-only strings (including the empty string). */
const whitespaceOnlyPromptArb = fc.string({ unit: whitespaceCharArb, maxLength: 30 });

/** Strings of exact length straddling the 4,000-character boundary. */
const boundaryPromptArb = fc
  .integer({ min: PROMPT_MAX_LENGTH - 3, max: PROMPT_MAX_LENGTH + 3 })
  .chain((length) => fc.string({ unit: charArb, minLength: length, maxLength: length }));

const promptArb = fc.oneof(
  { weight: 3, arbitrary: mixedPromptArb },
  { weight: 2, arbitrary: whitespaceOnlyPromptArb },
  { weight: 2, arbitrary: boundaryPromptArb }
);

/**
 * Independent oracle: at least one character survives a per-character trim,
 * and the total UTF-16 length is at most 4,000.
 */
function oracleAccepts(prompt: string): boolean {
  const hasNonWhitespace = [...prompt].some((ch) => ch.trim().length > 0);
  return hasNonWhitespace && prompt.length <= PROMPT_MAX_LENGTH;
}

describe('Property 1: Prompt validity predicate', () => {
  it('accepts iff at least one non-whitespace character and length ≤ 4,000', () => {
    fc.assert(
      fc.property(promptArb, (prompt) => {
        expect(isSubmittablePrompt(prompt)).toBe(oracleAccepts(prompt));
      }),
      { numRuns: 100 }
    );
  });

  it('the reducer never leaves idle on submit with a rejected prompt', () => {
    const rejectedPromptArb = promptArb.filter((prompt) => !oracleAccepts(prompt));
    const errorArb = fc.oneof(
      fc.constant(null),
      fc.record({
        header: fc.string({ maxLength: 20 }),
        message: fc.string({ maxLength: 40 }),
      })
    );

    fc.assert(
      fc.property(rejectedPromptArb, errorArb, (prompt, error) => {
        const state: CodeAssistState = { phase: 'idle', prompt, error };
        const next = codeAssistReducer(state, { type: 'submit' });

        // No invocation: the state is returned unchanged and stays idle.
        expect(next).toBe(state);
        expect(next.phase).toBe('idle');
      }),
      { numRuns: 100 }
    );
  });
});
