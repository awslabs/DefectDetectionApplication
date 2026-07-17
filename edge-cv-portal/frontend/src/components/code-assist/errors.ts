/**
 * Code_Assistant error presentation (custom-node-code-assist, task 6.2).
 *
 * Pure mapping from a failed `/code-assist` request to its inline-alert
 * presentation, modeled on the node-designer `describeGenerationError`
 * pattern: every failure is retryable — the panel keeps the prompt in
 * the input box so the user can resubmit without retyping
 * (Requirements 5.1, 5.2, 5.3).
 */
import { ApiError } from '../../services/api';

/** Presentation of one code-assist failure as a headed alert. */
export interface CodeAssistErrorView {
  header: string;
  message: string;
}

const GENERIC_HEADER = 'Code generation failed';
const GENERIC_MESSAGE = 'The code generation request failed.';

/**
 * Headers for 502 Bedrock failures, keyed by `details.category`
 * (Requirement 5.1: throttling / authorization / model-access /
 * model-error). Unknown categories fall back to the model-error header.
 */
const CATEGORY_HEADERS: Record<string, string> = {
  throttling: 'Throttled',
  authorization: 'Not authorized to invoke the model',
  'model-access': 'Model not available',
  'model-error': 'Model error',
};

/** The `category` detail of a Bedrock failure envelope, when present. */
function categoryOf(details: Record<string, unknown> | undefined): string | null {
  return details && typeof details.category === 'string' ? details.category : null;
}

/** Shared error-code + details.category -> alert-presentation mapping. */
function viewForCode(
  code: string | undefined,
  message: string | undefined,
  details: Record<string, unknown> | undefined
): CodeAssistErrorView {
  const text = message || GENERIC_MESSAGE;
  switch (code) {
    case 'BEDROCK_INVOCATION_FAILED':
    case 'BEDROCK_UNREACHABLE': {
      const category = categoryOf(details);
      return {
        header:
          (category && CATEGORY_HEADERS[category]) || CATEGORY_HEADERS['model-error'],
        message: text,
      };
    }
    case 'GENERATION_TIMEOUT': {
      // The applied (clamped) timeout in seconds (Requirement 5.2).
      const seconds =
        details && typeof details.timeout_seconds === 'number'
          ? details.timeout_seconds
          : null;
      return {
        header:
          seconds !== null ? `Timed out after ${seconds} seconds` : 'Generation timed out',
        message: text,
      };
    }
    case 'NO_CODE_RETURNED':
      return { header: 'No code produced', message: text };
    default:
      // Unknown codes (422 validation defects, 4xx request errors,
      // anything new) keep the server's message under a generic header.
      return { header: GENERIC_HEADER, message: text };
  }
}

/**
 * Map a failed code-assist request (a rejected `codeAssist` call or a
 * network error) to its alert presentation. The prompt is preserved by
 * the caller in every case (Requirements 5.1, 5.2, 5.3).
 */
export function describeCodeAssistError(err: unknown): CodeAssistErrorView {
  if (err instanceof ApiError) {
    return viewForCode(err.code, err.message, err.details);
  }
  return {
    header: GENERIC_HEADER,
    message: err instanceof Error && err.message ? err.message : GENERIC_MESSAGE,
  };
}
