/**
 * Unit tests for the Code_Assistant error presenter (custom-node-code-assist,
 * task 6.2, Requirements 5.1, 5.2, 5.3): the ApiError code + details.category
 * -> headed-alert mapping, the timeout-seconds echo, and the generic fallback
 * for unknown codes and non-ApiError failures.
 */
import { describe, expect, it } from 'vitest';

import { ApiError } from '../../services/api';
import { describeCodeAssistError } from './errors';

describe('describeCodeAssistError', () => {
  it('maps each Bedrock failure category to its header (5.1)', () => {
    const cases: Array<[string, string]> = [
      ['throttling', 'Throttled'],
      ['authorization', 'Not authorized to invoke the model'],
      ['model-access', 'Model not available'],
      ['model-error', 'Model error'],
    ];
    for (const [category, header] of cases) {
      const view = describeCodeAssistError(
        new ApiError('bedrock broke', 502, 'BEDROCK_INVOCATION_FAILED', { category })
      );
      expect(view.header).toBe(header);
      expect(view.message).toBe('bedrock broke');
    }
  });

  it('treats BEDROCK_UNREACHABLE as a categorized Bedrock failure (5.1)', () => {
    const view = describeCodeAssistError(
      new ApiError('cannot reach endpoint', 502, 'BEDROCK_UNREACHABLE', {
        region: 'us-east-1',
        category: 'model-access',
      })
    );
    expect(view.header).toBe('Model not available');
    expect(view.message).toBe('cannot reach endpoint');
  });

  it('falls back to the model-error header for missing or unknown categories (5.1)', () => {
    expect(
      describeCodeAssistError(new ApiError('broke', 502, 'BEDROCK_INVOCATION_FAILED')).header
    ).toBe('Model error');
    expect(
      describeCodeAssistError(
        new ApiError('broke', 502, 'BEDROCK_INVOCATION_FAILED', { category: 'novel' })
      ).header
    ).toBe('Model error');
  });

  it('states the applied timeout seconds from details.timeout_seconds (5.2)', () => {
    const view = describeCodeAssistError(
      new ApiError('Code generation timed out after 45 seconds.', 504, 'GENERATION_TIMEOUT', {
        timeout_seconds: 45,
        model_id: 'us.anthropic.claude',
      })
    );
    expect(view.header).toBe('Timed out after 45 seconds');
    expect(view.message).toBe('Code generation timed out after 45 seconds.');
  });

  it('falls back to a generic timeout header when the seconds detail is absent (5.2)', () => {
    expect(
      describeCodeAssistError(new ApiError('timed out', 504, 'GENERATION_TIMEOUT')).header
    ).toBe('Generation timed out');
  });

  it('describes an empty generation result as no code produced (5.3)', () => {
    const view = describeCodeAssistError(
      new ApiError('The model returned no code.', 422, 'NO_CODE_RETURNED', {
        stop_reason: 'end_turn',
      })
    );
    expect(view.header).toBe('No code produced');
    expect(view.message).toBe('The model returned no code.');
  });

  it('keeps the server message under a generic header for unknown codes', () => {
    for (const code of ['MISSING_ENTRY_POINT', 'GENERATED_CODE_INVALID', 'FORBIDDEN', 'BRAND_NEW']) {
      const view = describeCodeAssistError(new ApiError('server said so', 422, code));
      expect(view.header).toBe('Code generation failed');
      expect(view.message).toBe('server said so');
    }
  });

  it('handles plain errors and non-Error values without losing the message', () => {
    expect(describeCodeAssistError(new Error('network down')).message).toBe('network down');
    expect(describeCodeAssistError(new Error('network down')).header).toBe(
      'Code generation failed'
    );
    expect(describeCodeAssistError('boom').message).toBe('The code generation request failed.');
    expect(describeCodeAssistError(undefined).header).toBe('Code generation failed');
  });
});
