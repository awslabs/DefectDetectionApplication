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
 * Longest Diagnostic_Context text sent to the generator, in characters
 * (custom-node-source-lifecycle 5.4). Longer error output keeps its tail.
 */
export const DIAGNOSTICS_MAX_LENGTH = 16 * 1024;

/** Keep the last DIAGNOSTICS_MAX_LENGTH characters; report truncation. */
export function truncateDiagnostics(text: string): { text: string; truncated: boolean } {
  if (text.length <= DIAGNOSTICS_MAX_LENGTH) {
    return { text, truncated: false };
  }
  return { text: text.slice(text.length - DIAGNOSTICS_MAX_LENGTH), truncated: true };
}

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

/**
 * Error output attached to the next submission (custom-node-source-
 * lifecycle 5.1-5.4): seeded by "Fix with AI" (build/simulation) or pasted
 * by the user. Carried through idle/submitting so a failure preserves it
 * together with the prompt; cleared on accept.
 */
export interface CodeAssistDiagnosticsState {
  kind: 'build' | 'simulation' | 'user';
  architecture?: string;
  text: string;
}

export type CodeAssistState =
  | {
      phase: 'idle';
      prompt: string;
      error: CodeAssistErrorView | null;
      diagnostics: CodeAssistDiagnosticsState | null;
    }
  | { phase: 'submitting'; prompt: string; diagnostics: CodeAssistDiagnosticsState | null }
  | {
      phase: 'reviewing';
      prompt: string;
      diagnostics: CodeAssistDiagnosticsState | null;
      code: string;
      notes: string;
      /** Source_Tree file the code applies to; null = the active file (5.7). */
      targetFile: string | null;
    };

export type CodeAssistEvent =
  | { type: 'edit-prompt'; value: string }
  | { type: 'edit-diagnostics'; diagnostics: CodeAssistDiagnosticsState | null }
  | { type: 'submit' }
  | { type: 'succeeded'; code: string; notes: string; targetFile?: string | null }
  | { type: 'failed'; error: CodeAssistErrorView }
  | { type: 'accept' }
  | { type: 'reject' };

/** The panel's starting state: idle, empty prompt, no error shown. */
export const INITIAL_CODE_ASSIST_STATE: CodeAssistState = {
  phase: 'idle',
  prompt: '',
  error: null,
  diagnostics: null,
};

/** Normalize a diagnostics edit: blank text clears the attachment. */
function normalizeDiagnostics(
  diagnostics: CodeAssistDiagnosticsState | null
): CodeAssistDiagnosticsState | null {
  if (!diagnostics || !diagnostics.text.trim()) return null;
  return diagnostics;
}

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
        ? { phase: 'idle', prompt: event.value, error: state.error, diagnostics: state.diagnostics }
        : state;

    case 'edit-diagnostics':
      return state.phase === 'idle'
        ? {
            phase: 'idle',
            prompt: state.prompt,
            error: state.error,
            diagnostics: normalizeDiagnostics(event.diagnostics),
          }
        : state;

    case 'submit':
      return state.phase === 'idle' && isSubmittablePrompt(state.prompt)
        ? { phase: 'submitting', prompt: state.prompt, diagnostics: state.diagnostics }
        : state;

    case 'succeeded':
      return state.phase === 'submitting'
        ? {
            phase: 'reviewing',
            prompt: state.prompt,
            diagnostics: state.diagnostics,
            code: event.code,
            notes: event.notes,
            targetFile: event.targetFile ?? null,
          }
        : state;

    case 'failed':
      return state.phase === 'submitting'
        ? {
            phase: 'idle',
            prompt: state.prompt,
            error: event.error,
            diagnostics: state.diagnostics,
          }
        : state;

    case 'accept':
      return state.phase === 'reviewing'
        ? { phase: 'idle', prompt: '', error: null, diagnostics: null }
        : state;

    case 'reject':
      return state.phase === 'reviewing'
        ? { phase: 'idle', prompt: state.prompt, error: null, diagnostics: state.diagnostics }
        : state;
  }
}
