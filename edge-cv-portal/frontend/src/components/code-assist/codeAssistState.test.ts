/**
 * Unit tests for the CodeAssistPanel state machine
 * (Requirements 1.4, 1.6, 2.8, 2.9, 5.5).
 */

import { describe, expect, it } from 'vitest';
import {
  codeAssistReducer,
  INITIAL_CODE_ASSIST_STATE,
  isSubmittablePrompt,
  PROMPT_MAX_LENGTH,
  type CodeAssistState,
} from './codeAssistState';

const ERROR = { header: 'Throttled', message: 'try again' };

function idleWithPrompt(prompt: string): CodeAssistState {
  return codeAssistReducer(INITIAL_CODE_ASSIST_STATE, { type: 'edit-prompt', value: prompt });
}

function submittingWith(prompt: string): CodeAssistState {
  return codeAssistReducer(idleWithPrompt(prompt), { type: 'submit' });
}

function reviewingWith(prompt: string, code: string, notes: string): CodeAssistState {
  return codeAssistReducer(submittingWith(prompt), { type: 'succeeded', code, notes });
}

describe('isSubmittablePrompt', () => {
  it('accepts a plain prompt', () => {
    expect(isSubmittablePrompt('blur the frame')).toBe(true);
  });

  it('rejects empty and whitespace-only prompts (Requirements 1.4, 2.8)', () => {
    expect(isSubmittablePrompt('')).toBe(false);
    expect(isSubmittablePrompt('   \n\t ')).toBe(false);
  });

  it('accepts exactly 4,000 characters and rejects 4,001 (Requirement 1.4)', () => {
    expect(isSubmittablePrompt('a'.repeat(PROMPT_MAX_LENGTH))).toBe(true);
    expect(isSubmittablePrompt('a'.repeat(PROMPT_MAX_LENGTH + 1))).toBe(false);
  });

  it('rejects a padded prompt whose total length exceeds the limit', () => {
    expect(isSubmittablePrompt(`${'a'.repeat(PROMPT_MAX_LENGTH)} `)).toBe(false);
  });
});

describe('codeAssistReducer', () => {
  it('starts idle with an empty prompt and no error', () => {
    expect(INITIAL_CODE_ASSIST_STATE).toEqual({ phase: 'idle', prompt: '', error: null });
  });

  it('edits the prompt while idle and keeps the current error view', () => {
    const failed = codeAssistReducer(submittingWith('p'), { type: 'failed', error: ERROR });
    const edited = codeAssistReducer(failed, { type: 'edit-prompt', value: 'q' });
    expect(edited).toEqual({ phase: 'idle', prompt: 'q', error: ERROR });
  });

  it('submit moves idle -> submitting with the same prompt', () => {
    expect(submittingWith('sharpen')).toEqual({ phase: 'submitting', prompt: 'sharpen' });
  });

  it('submit is ignored on an unsubmittable prompt (Requirements 1.4, 2.8)', () => {
    const state = idleWithPrompt('   ');
    expect(codeAssistReducer(state, { type: 'submit' })).toBe(state);
  });

  it('submit is ignored while submitting and while reviewing (Requirement 1.6)', () => {
    const submitting = submittingWith('p');
    expect(codeAssistReducer(submitting, { type: 'submit' })).toBe(submitting);
    const reviewing = reviewingWith('p', 'code', 'notes');
    expect(codeAssistReducer(reviewing, { type: 'submit' })).toBe(reviewing);
  });

  it('edit-prompt is ignored while submitting so failure restores the submitted prompt', () => {
    const submitting = submittingWith('p');
    expect(codeAssistReducer(submitting, { type: 'edit-prompt', value: 'x' })).toBe(submitting);
  });

  it('succeeded moves submitting -> reviewing carrying code and notes', () => {
    expect(reviewingWith('p', 'def process_frame(f, m): ...', 'notes')).toEqual({
      phase: 'reviewing',
      prompt: 'p',
      code: 'def process_frame(f, m): ...',
      notes: 'notes',
    });
  });

  it('failed returns to idle with the same prompt and the error view (Requirement 5.5)', () => {
    const failed = codeAssistReducer(submittingWith('p'), { type: 'failed', error: ERROR });
    expect(failed).toEqual({ phase: 'idle', prompt: 'p', error: ERROR });
  });

  it('accept returns to idle with the prompt cleared', () => {
    const accepted = codeAssistReducer(reviewingWith('p', 'c', 'n'), { type: 'accept' });
    expect(accepted).toEqual({ phase: 'idle', prompt: '', error: null });
  });

  it('reject returns to idle with the same prompt (Requirement 2.9)', () => {
    const rejected = codeAssistReducer(reviewingWith('p', 'c', 'n'), { type: 'reject' });
    expect(rejected).toEqual({ phase: 'idle', prompt: 'p', error: null });
  });

  it('succeeded/failed outside submitting and accept/reject outside reviewing are no-ops', () => {
    const idle = idleWithPrompt('p');
    expect(codeAssistReducer(idle, { type: 'succeeded', code: 'c', notes: 'n' })).toBe(idle);
    expect(codeAssistReducer(idle, { type: 'failed', error: ERROR })).toBe(idle);
    expect(codeAssistReducer(idle, { type: 'accept' })).toBe(idle);
    expect(codeAssistReducer(idle, { type: 'reject' })).toBe(idle);
    const submitting = submittingWith('p');
    expect(codeAssistReducer(submitting, { type: 'accept' })).toBe(submitting);
    expect(codeAssistReducer(submitting, { type: 'reject' })).toBe(submitting);
  });
});
