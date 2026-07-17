/**
 * CodeAssistPanel state machine (custom-node-code-assist, task 6.1).
 *
 * Pure reducer behind the shared CodeAssistPanel: the idle/submitting/
 * reviewing phases over edit-prompt/submit/succeeded/failed/accept/reject
 * events, kept out of the component so the prompt-preservation and
 * single-submission rules (Requirements 1.4, 1.6, 2.8, 2.9, 5.5) are
 * property-testable in isolation. No React here: components own the side
 * effects (the API call on submit, onAccept on accept) and feed the
 * results back in as events.
 */

// ----------------------------------------------------------- prompt rules

/** Longest accepted prompt, in characters (Requirement 1.4). */
export const PROMPT_MAX_LENGTH = 4_000;

/**
 * True when the prompt may be submitted: at least one non-whitespace
 * character and a total length of at most 4,000 characters
 * (Requirements 1.4, 2.8).
 */
export function isSubmittablePrompt(prompt: string): boolean {
  return prompt.trim().length >= 1 && prompt.length <= PROMPT_MAX_LENGTH;
}

// ----------------------------------------------------------------- state

/**
 * Presentation of one code-assist failure, rendered as an inline Alert.
 * Produced by describeCodeAssistError (task 6.2); the reducer only
 * carries it (Requirements 5.1, 5.2, 5.3).
 */
export interface CodeAssistErrorView {
  header: string;
  message: string;
}

export type CodeAssistState =
  | { phase: 'idle'; prompt: string; error: CodeAssistErrorView | null }
  | { phase: 'submitting'; prompt: string }
  | { phase: 'reviewing'; prompt: string; code: string; notes: string };

export type CodeAssistEvent =
  | { type: 'edit-prompt'; value: string }
  | { type: 'submit' }
  | { type: 'succeeded'; code: string; notes: string }
  | { type: 'failed'; error: CodeAssistErrorView }
  | { type: 'accept' }
  | { type: 'reject' };

/** The panel's starting state: idle, empty prompt, no error shown. */
export const INITIAL_CODE_ASSIST_STATE: CodeAssistState = {
  phase: 'idle',
  prompt: '',
  error: null,
};

// --------------------------------------------------------------- reducer

/**
 * Pure transition function. Events that do not apply to the current
 * phase leave the state unchanged, so:
 *
 * - `submit` is ignored unless idle with a submittable prompt — a
 *   rejected prompt never leaves idle and an in-flight invocation
 *   cannot be doubled up (Requirements 1.4, 1.6, 2.8);
 * - `failed` returns to idle with the prompt unchanged from submission
 *   and the error view for the inline Alert (Requirement 5.5);
 * - `reject` returns to idle with the prompt preserved and the editor
 *   untouched (Requirement 2.9);
 * - `accept` returns to idle with the prompt cleared — the component
 *   fires onAccept(code); the reducer itself has no side effects.
 */
export function codeAssistReducer(
  state: CodeAssistState,
  event: CodeAssistEvent
): CodeAssistState {
  switch (event.type) {
    case 'edit-prompt':
      // The prompt is editable only while idle; during submission it is
      // frozen so a failure restores exactly what was submitted (5.5).
      return state.phase === 'idle'
        ? { phase: 'idle', prompt: event.value, error: state.error }
        : state;

    case 'submit':
      return state.phase === 'idle' && isSubmittablePrompt(state.prompt)
        ? { phase: 'submitting', prompt: state.prompt }
        : state;

    case 'succeeded':
      return state.phase === 'submitting'
        ? {
            phase: 'reviewing',
            prompt: state.prompt,
            code: event.code,
            notes: event.notes,
          }
        : state;

    case 'failed':
      return state.phase === 'submitting'
        ? { phase: 'idle', prompt: state.prompt, error: event.error }
        : state;

    case 'accept':
      return state.phase === 'reviewing'
        ? { phase: 'idle', prompt: '', error: null }
        : state;

    case 'reject':
      return state.phase === 'reviewing'
        ? { phase: 'idle', prompt: state.prompt, error: null }
        : state;
  }
}
