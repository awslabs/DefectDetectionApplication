/**
 * Generate-panel helpers (custom-node-designer, task 12.2).
 *
 * Pure chat-state, poll-state, and error-presentation logic behind
 * GeneratePanel, kept out of the component so the start/poll turn
 * derivation and the prompt-preservation and error rendering rules
 * (Requirements 2.6, 2.7) are unit-testable.
 */
import { ApiError } from '../../services/api';
import type { GenerationTurnError, GenerationTurnStatus } from './types';

// ------------------------------------------------------------- poll state

/**
 * Poll the generation turn every 4 s while it runs (generation takes
 * 45-50 s; same start/poll pattern as the Plugin_Simulator view).
 */
export const GENERATION_POLL_MS = 4_000;

/** True when the polled turn has settled (completed or failed). */
export function isTerminalTurn(
  status: GenerationTurnStatus | string | undefined
): status is 'completed' | 'failed' {
  return status === 'completed' || status === 'failed';
}

// ------------------------------------------------------------ chat state

/** One rendered turn of the generation chat (2.1). */
export interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
}

/**
 * Append a completed generation turn to the chat transcript. Only
 * successful turns reach the transcript: a failed turn leaves the
 * transcript untouched and the prompt in the input box (2.6, 2.7).
 */
export function appendTurn(
  messages: ChatMessage[],
  prompt: string,
  assistantText: string | null | undefined
): ChatMessage[] {
  return [
    ...messages,
    { role: 'user', text: prompt },
    {
      role: 'assistant',
      text:
        (assistantText || '').trim() ||
        'Generated the Plugin_Scaffold source shown below.',
    },
  ];
}

// -------------------------------------------------------- error rendering

/** Presentation of one generation failure (2.6, 2.7). */
export interface GenerationErrorView {
  header: string;
  message: string;
  /** Scaffold-validation defects (422 GENERATED_SCAFFOLD_INVALID). */
  defects: string[];
}

/** Only string entries of a details.defects list are rendered. */
function stringDefects(details: Record<string, unknown> | undefined): string[] {
  return details && Array.isArray(details.defects)
    ? (details.defects as unknown[]).filter((d): d is string => typeof d === 'string')
    : [];
}

/** Shared error-code -> alert-presentation mapping (2.6, 2.7). */
function viewForCode(
  code: string | undefined,
  message: string | undefined,
  defects: string[]
): GenerationErrorView {
  const text = message || 'The generation request failed.';
  switch (code) {
    case 'GENERATED_SCAFFOLD_INVALID':
      return {
        header: 'Generated source is not a buildable Plugin_Scaffold',
        message: text,
        defects,
      };
    case 'GENERATION_TIMEOUT':
      return { header: 'Generation timed out', message: text, defects: [] };
    case 'BEDROCK_UNREACHABLE':
    case 'BEDROCK_INVOCATION_FAILED':
    case 'NO_SCAFFOLD_RETURNED':
      return { header: 'Generation failed', message: text, defects: [] };
    default:
      return { header: 'Generation failed', message: text, defects };
  }
}

/**
 * Map a generation request failure (a rejected start/poll call, e.g.
 * 400 INVALID_DECLARATION or a network error) to its alert
 * presentation. Every failure is retryable: the caller keeps the prompt
 * in the input box so the user can retry or rephrase without retyping
 * (Requirements 2.6, 2.7).
 */
export function describeGenerationError(err: unknown): GenerationErrorView {
  if (err instanceof ApiError) {
    return viewForCode(err.code, err.message, stringDefects(err.details));
  }
  return {
    header: 'Generation failed',
    message:
      err instanceof Error && err.message
        ? err.message
        : 'The generation request failed.',
    defects: [],
  };
}

/**
 * Map a failed generation turn's turn_error (from the poll response) to
 * its alert presentation - the same rules as describeGenerationError,
 * since the poll carries the former synchronous error envelope contents
 * (2.6, 2.7).
 */
export function describeTurnError(
  error: GenerationTurnError | null | undefined
): GenerationErrorView {
  return viewForCode(error?.code, error?.message, stringDefects(error?.details));
}
