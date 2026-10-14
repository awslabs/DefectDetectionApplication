/**
 * Unit tests for the generate-panel helpers (custom-node-designer
 * task 12.2, Requirements 2.3, 2.6, 2.7): chat-transcript assembly,
 * start/poll terminal-state detection, and error mapping for both
 * rejected requests (describeGenerationError) and failed polled turns
 * (describeTurnError).
 */
import { describe, expect, it } from 'vitest';
import { ApiError } from '../../services/api';
import {
  appendTurn,
  describeGenerationError,
  describeTurnError,
  isTerminalTurn,
} from './generate';

describe('appendTurn', () => {
  it('appends the user prompt and the assistant commentary in order', () => {
    const messages = appendTurn(
      [{ role: 'user' as const, text: 'first' }, { role: 'assistant' as const, text: 'ok' }],
      'make it faster',
      'Reduced the per-frame work.'
    );
    expect(messages).toHaveLength(4);
    expect(messages[2]).toEqual({ role: 'user', text: 'make it faster' });
    expect(messages[3]).toEqual({ role: 'assistant', text: 'Reduced the per-frame work.' });
  });

  it('does not mutate the existing transcript', () => {
    const original = [{ role: 'user' as const, text: 'first' }];
    appendTurn(original, 'second', 'done');
    expect(original).toHaveLength(1);
  });

  it('substitutes a default note when the model returned no commentary', () => {
    const messages = appendTurn([], 'blur the frame', '');
    expect(messages[1].role).toBe('assistant');
    expect(messages[1].text.length).toBeGreaterThan(0);
    expect(appendTurn([], 'p', null)[1].text).toBe(messages[1].text);
  });
});

describe('describeGenerationError', () => {
  it('surfaces scaffold-validation defects from GENERATED_SCAFFOLD_INVALID (2.6)', () => {
    const view = describeGenerationError(
      new ApiError('not buildable', 422, 'GENERATED_SCAFFOLD_INVALID', {
        defects: ['missing hook file', 'empty meson.build'],
      })
    );
    expect(view.header).toContain('not a buildable');
    expect(view.message).toBe('not buildable');
    expect(view.defects).toEqual(['missing hook file', 'empty meson.build']);
  });

  it('describes a Bedrock timeout with the backend message (2.7)', () => {
    const view = describeGenerationError(
      new ApiError('Scaffold generation timed out after 60 seconds.', 504, 'GENERATION_TIMEOUT')
    );
    expect(view.header).toBe('Generation timed out');
    expect(view.message).toContain('timed out');
    expect(view.defects).toEqual([]);
  });

  it('describes Bedrock invocation failures with the backend message (2.7)', () => {
    for (const code of ['BEDROCK_UNREACHABLE', 'BEDROCK_INVOCATION_FAILED', 'NO_SCAFFOLD_RETURNED']) {
      const view = describeGenerationError(new ApiError('bedrock broke', 502, code));
      expect(view.header).toBe('Generation failed');
      expect(view.message).toBe('bedrock broke');
    }
  });

  it('handles unknown error codes and plain errors without losing the message', () => {
    expect(describeGenerationError(new ApiError('denied', 403, 'FORBIDDEN')).message).toBe('denied');
    expect(describeGenerationError(new Error('network down')).message).toBe('network down');
    expect(describeGenerationError('boom').message).toBe('The generation request failed.');
  });

  it('ignores non-string entries in the defects detail', () => {
    const view = describeGenerationError(
      new ApiError('bad', 422, 'GENERATED_SCAFFOLD_INVALID', {
        defects: ['real defect', 42, null],
      })
    );
    expect(view.defects).toEqual(['real defect']);
  });
});

describe('isTerminalTurn', () => {
  it('treats completed and failed as terminal poll states', () => {
    expect(isTerminalTurn('completed')).toBe(true);
    expect(isTerminalTurn('failed')).toBe(true);
  });

  it('keeps polling for pending, running, and unknown states', () => {
    expect(isTerminalTurn('pending')).toBe(false);
    expect(isTerminalTurn('running')).toBe(false);
    expect(isTerminalTurn(undefined)).toBe(false);
    expect(isTerminalTurn('something-else')).toBe(false);
  });
});

describe('describeTurnError', () => {
  it('surfaces scaffold-validation defects from a failed polled turn (2.6)', () => {
    const view = describeTurnError({
      code: 'GENERATED_SCAFFOLD_INVALID',
      message: 'not buildable',
      details: { defects: ['missing hook file', 42, null] },
      http_status: 422,
    });
    expect(view.header).toContain('not a buildable');
    expect(view.message).toBe('not buildable');
    expect(view.defects).toEqual(['missing hook file']);
  });

  it('maps timeout and Bedrock failure codes like the request-error path (2.7)', () => {
    expect(
      describeTurnError({ code: 'GENERATION_TIMEOUT', message: 'timed out after 60 seconds' })
        .header
    ).toBe('Generation timed out');
    for (const code of ['BEDROCK_UNREACHABLE', 'BEDROCK_INVOCATION_FAILED', 'NO_SCAFFOLD_RETURNED']) {
      const view = describeTurnError({ code, message: 'bedrock broke' });
      expect(view.header).toBe('Generation failed');
      expect(view.message).toBe('bedrock broke');
    }
  });

  it('falls back to a generic message for unknown or missing errors', () => {
    expect(describeTurnError(null).message).toBe('The generation request failed.');
    expect(describeTurnError(undefined).header).toBe('Generation failed');
    expect(describeTurnError({ code: 'SOMETHING_NEW', message: 'boom' }).message).toBe('boom');
  });
});
