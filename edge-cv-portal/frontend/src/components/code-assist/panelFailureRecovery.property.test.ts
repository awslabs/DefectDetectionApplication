/**
 * **Feature: custom-node-code-assist, Property 13: Panel failure recovery preserves the prompt**
 *
 * For any sequence of CodeAssistPanel reducer events, whenever a `failed`
 * event is processed the resulting state is `idle` with the prompt string
 * unchanged from the moment of submission and an error view present; a
 * `reject` event likewise returns to `idle` with the prompt unchanged;
 * `submit` is a no-op except from `idle` with a submittable prompt; and
 * accept-callback effects occur only on `accept` from `reviewing`.
 *
 * **Validates: Requirements 1.6, 2.9, 5.1, 5.2, 5.3, 5.5**
 *
 * The test folds random event sequences through the pure reducer, recording
 * the prompt at each effective submission, and checks every transition
 * against the state-machine rules. The accept callback is modeled the way
 * CodeAssistPanel fires it — code can leave the panel only when an `accept`
 * event transitions out of `reviewing`; from every other state, `accept` is
 * asserted to change nothing, so no code exists to apply.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  codeAssistReducer,
  isSubmittablePrompt,
  INITIAL_CODE_ASSIST_STATE,
  PROMPT_MAX_LENGTH,
  type CodeAssistEvent,
  type CodeAssistState,
} from './codeAssistState';

// ------------------------------------------------------------- generators

const whitespaceCharArb = fc.constantFrom(' ', '\t', '\n', '\r', '\u00a0', '\u3000');
const nonWhitespaceCharArb = fc.constantFrom(...'abcXYZ019._-', 'é', '日', 'π');
const charArb = fc.oneof(whitespaceCharArb, nonWhitespaceCharArb);

/**
 * Prompt values for edit-prompt events: mixed short strings, whitespace-only
 * strings, and occasional over-length strings, so sequences exercise both
 * submittable and rejected prompts.
 */
const promptValueArb = fc.oneof(
  { weight: 4, arbitrary: fc.string({ unit: charArb, maxLength: 20 }) },
  { weight: 2, arbitrary: fc.string({ unit: whitespaceCharArb, maxLength: 10 }) },
  { weight: 1, arbitrary: fc.constant('x'.repeat(PROMPT_MAX_LENGTH + 1)) }
);

const errorViewArb = fc.record({
  header: fc.string({ maxLength: 15 }),
  message: fc.string({ maxLength: 30 }),
});

const eventArb: fc.Arbitrary<CodeAssistEvent> = fc.oneof(
  {
    weight: 3,
    arbitrary: promptValueArb.map(
      (value): CodeAssistEvent => ({ type: 'edit-prompt', value })
    ),
  },
  { weight: 3, arbitrary: fc.constant<CodeAssistEvent>({ type: 'submit' }) },
  {
    weight: 2,
    arbitrary: fc
      .record({ code: fc.string({ maxLength: 30 }), notes: fc.string({ maxLength: 20 }) })
      .map(({ code, notes }): CodeAssistEvent => ({ type: 'succeeded', code, notes })),
  },
  {
    weight: 2,
    arbitrary: errorViewArb.map((error): CodeAssistEvent => ({ type: 'failed', error })),
  },
  { weight: 1, arbitrary: fc.constant<CodeAssistEvent>({ type: 'accept' }) },
  { weight: 1, arbitrary: fc.constant<CodeAssistEvent>({ type: 'reject' }) }
);

const eventSequenceArb = fc.array(eventArb, { maxLength: 50 });

// ---------------------------------------------------------------- property

describe('Property 13: Panel failure recovery preserves the prompt', () => {
  it('failed/reject preserve the submitted prompt; submit and accept fire only from their phases', () => {
    fc.assert(
      fc.property(eventSequenceArb, (events) => {
        let state: CodeAssistState = INITIAL_CODE_ASSIST_STATE;
        // The prompt as it stood at the most recent effective submission.
        let promptAtSubmission: string | null = null;

        for (const event of events) {
          const prev = state;
          const next = codeAssistReducer(prev, event);

          switch (event.type) {
            case 'submit':
              // A no-op except from idle with a submittable prompt (1.4/2.8
              // guard restated by this property).
              if (prev.phase === 'idle' && isSubmittablePrompt(prev.prompt)) {
                expect(next).toEqual({ phase: 'submitting', prompt: prev.prompt });
                promptAtSubmission = prev.prompt;
              } else {
                expect(next).toBe(prev);
              }
              break;

            case 'failed':
              if (prev.phase === 'submitting') {
                // Failure recovery: idle, prompt unchanged from submission,
                // error view present (1.6, 5.1, 5.2, 5.3, 5.5).
                expect(next.phase).toBe('idle');
                expect(next.prompt).toBe(promptAtSubmission);
                if (next.phase === 'idle') {
                  expect(next.error).toEqual(event.error);
                  expect(next.error).not.toBeNull();
                }
              } else {
                expect(next).toBe(prev);
              }
              break;

            case 'reject':
              if (prev.phase === 'reviewing') {
                // Reject returns to idle with the prompt unchanged (2.9).
                expect(next.phase).toBe('idle');
                expect(next.prompt).toBe(prev.prompt);
                expect(next.prompt).toBe(promptAtSubmission);
              } else {
                expect(next).toBe(prev);
              }
              break;

            case 'accept':
              if (prev.phase === 'reviewing') {
                // The only accept-callback site: the component fires
                // onAccept(prev.code) exactly on this transition, and the
                // prompt is cleared.
                expect(typeof prev.code).toBe('string');
                expect(next).toEqual({ phase: 'idle', prompt: '', error: null });
              } else {
                // From any other phase, accept changes nothing — there is no
                // reviewed code, so no accept-callback effect can occur.
                expect(next).toBe(prev);
              }
              break;

            case 'succeeded':
              if (prev.phase === 'submitting') {
                // The prompt survives into review, so a later reject/failed
                // comparison against the submitted prompt is meaningful.
                expect(next).toEqual({
                  phase: 'reviewing',
                  prompt: promptAtSubmission,
                  code: event.code,
                  notes: event.notes,
                });
              } else {
                expect(next).toBe(prev);
              }
              break;

            case 'edit-prompt':
              // Editing is possible only while idle; in-flight and reviewing
              // prompts stay frozen so recovery restores the submitted text.
              if (prev.phase === 'idle') {
                expect(next).toEqual({
                  phase: 'idle',
                  prompt: event.value,
                  error: prev.error,
                });
              } else {
                expect(next).toBe(prev);
              }
              break;
          }

          state = next;
        }
      }),
      { numRuns: 100 }
    );
  });
});
