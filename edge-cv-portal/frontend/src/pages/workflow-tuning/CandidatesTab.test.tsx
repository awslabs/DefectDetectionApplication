/**
 * Unit tests for the Candidates tab's preview, its warnings and the
 * read-only baseline (quality-prompt-tuning, task 8.6 — Requirement 5.3,
 * with 5.1, 5.4, 5.5 and 5.6 as the surrounding editor behaviour).
 *
 * Two preview sources are checked, because Requirement 5.3 asks for the
 * exact text *while* editing:
 *
 *  - the SAVED Candidate's preview is the route's answer verbatim
 *    (`GET .../candidates/{cid}/preview` is authoritative);
 *  - an unsaved draft is projected locally from the Invocation_Builder's two
 *    rules (`promptPreview.ts`), flagged as a draft, and replaced by the
 *    route's answer as soon as the draft is saved.
 *
 * The projection helpers and the draft warning rules are unit-tested
 * directly too, against the wording of `workflow_tuning.preview_warnings`.
 *
 * No AWS and no network: `apiService` is mocked.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import CandidatesTab, {
  BASELINE_READ_ONLY_NOTE,
  DRAFT_PREVIEW_NOTE,
} from './CandidatesTab';
import {
  DEFAULT_MAX_TOKENS,
  INSTRUCTION_SEPARATOR,
  MIN_SAFE_MAX_TOKENS,
  STARTER_CANDIDATE,
  VERDICT_INSTRUCTION,
  draftWarnings,
  projectMaxTokens,
  projectSystemText,
  projectUserMessage,
} from './promptPreview';
import type {
  CandidatePreviewResponse,
  TuningCandidate,
  TuningNodeView,
} from './types';

const {
  getTuningCandidatePreview,
  createTuningCandidate,
  updateTuningCandidate,
  deleteTuningCandidate,
} = vi.hoisted(() => ({
  getTuningCandidatePreview: vi.fn(),
  createTuningCandidate: vi.fn(),
  updateTuningCandidate: vi.fn(),
  deleteTuningCandidate: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getTuningCandidatePreview,
    createTuningCandidate,
    updateTuningCandidate,
    deleteTuningCandidate,
  },
}));

// ------------------------------------------------------------------ fixtures

const NODE: TuningNodeView = {
  nodeId: 'bedrock_1',
  nodeType: 'bedrock_inference',
  model: 'anthropic.claude-3-5-sonnet',
  parameters: { anomaly_mode: true },
  promptSet: { prompt: 'Inspect the part.', systemPrompt: null, maxTokens: 256 },
  maxTokensBounds: { min: 1, max: 4096 },
};

function candidate(patch: Partial<TuningCandidate> = {}): TuningCandidate {
  return {
    candidateId: 'cand-1',
    name: 'Candidate 1',
    prompt: 'Compare the two images.',
    systemPrompt: null,
    maxTokens: 512,
    isBaseline: false,
    latestRun: null,
    ...patch,
  };
}

const BASELINE: TuningCandidate = candidate({
  candidateId: 'baseline',
  name: 'Baseline (deployed)',
  prompt: 'Inspect the part.',
  isBaseline: true,
  baselineVersion: 4,
});

function preview(
  patch: Partial<CandidatePreviewResponse> = {}
): CandidatePreviewResponse {
  return {
    candidateId: 'cand-1',
    sessionId: 'ts-1',
    nodeType: 'bedrock_inference',
    userMessage: `Compare the two images.${INSTRUCTION_SEPARATOR}${VERDICT_INSTRUCTION}`,
    systemText: null,
    maxTokens: 512,
    model: 'anthropic.claude-3-5-sonnet',
    templateRendered: true,
    warnings: [],
    ...patch,
  };
}

async function renderTab(
  candidates: TuningCandidate[] = [BASELINE, candidate()],
  node: TuningNodeView | null = NODE
) {
  const onChanged = vi.fn();
  render(
    <CandidatesTab
      sessionId="ts-1"
      node={node}
      candidates={candidates}
      onChanged={onChanged}
    />
  );
  await waitFor(() => expect(getTuningCandidatePreview).toHaveBeenCalled());
  return { onChanged };
}

function typeInto(testId: string, value: string) {
  const field = screen.getByTestId(testId);
  const input =
    field.querySelector('textarea') ?? (field.querySelector('input') as HTMLElement);
  fireEvent.change(input, { target: { value } });
}

beforeEach(() => {
  vi.clearAllMocks();
  getTuningCandidatePreview.mockResolvedValue(preview());
  updateTuningCandidate.mockImplementation(async (_s, _c, body) => ({
    candidate: candidate(body as Partial<TuningCandidate>),
  }));
  createTuningCandidate.mockImplementation(async (_s, body) => ({
    candidate: candidate({ candidateId: 'cand-new', ...(body as object) }),
  }));
});

afterEach(() => {
  cleanup();
});

// ------------------------------ the projection helpers (Requirements 5.3-5.5)

describe('the Invocation_Builder projection used for an unsaved draft', () => {
  it('appends the Verdict_Instruction after a blank line', () => {
    expect(projectUserMessage('Inspect it.')).toBe(
      `Inspect it.\n\n${VERDICT_INSTRUCTION}`
    );
    expect(INSTRUCTION_SEPARATOR).toBe('\n\n');
    expect(VERDICT_INSTRUCTION).toBe(
      'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'
    );
  });

  it('sends a whitespace-only system prompt as no system text, and anything else verbatim', () => {
    expect(projectSystemText(null)).toBeNull();
    expect(projectSystemText(undefined)).toBeNull();
    expect(projectSystemText('   \n\t ')).toBeNull();
    expect(projectSystemText('  You are an inspector.  ')).toBe(
      '  You are an inspector.  '
    );
  });

  it('falls back to the builder default token budget on a falsy max_tokens', () => {
    expect(projectMaxTokens(512)).toBe(512);
    expect(projectMaxTokens(null)).toBe(DEFAULT_MAX_TOKENS);
    expect(projectMaxTokens(0)).toBe(DEFAULT_MAX_TOKENS);
    expect(DEFAULT_MAX_TOKENS).toBe(256);
  });

  it('warns below 64 tokens and not at or above it (Requirement 5.5)', () => {
    expect(MIN_SAFE_MAX_TOKENS).toBe(64);
    expect(draftWarnings('Inspect it.', null, 32).map((w) => w.code)).toEqual([
      'max_tokens_truncation',
    ]);
    expect(draftWarnings('Inspect it.', null, 63)[0].message).toContain(
      'max_tokens is 63'
    );
    expect(draftWarnings('Inspect it.', null, 64)).toEqual([]);
    expect(draftWarnings('Inspect it.', null, null)).toEqual([]);
  });

  it('warns when a demanded answer format omits is_anomalous (Requirement 5.4)', () => {
    const jsonPrompt = 'Answer as JSON with a "verdict" field.';
    const warned = draftWarnings(jsonPrompt, null, 256);
    expect(warned.map((w) => w.code)).toEqual([
      'answer_schema_missing_is_anomalous',
    ]);
    expect(warned[0].field).toBe('prompt');
    // A braces-only schema counts as demanding a format.
    expect(
      draftWarnings('Reply with {"verdict": "..."}', null, 256).map((w) => w.code)
    ).toEqual(['answer_schema_missing_is_anomalous']);
    // The system prompt is checked too, and named.
    expect(draftWarnings('Inspect it.', 'Reply in JSON.', 256)[0].field).toBe(
      'systemPrompt'
    );
    // Mentioning is_anomalous silences it.
    expect(
      draftWarnings('Answer as JSON with is_anomalous.', null, 256)
    ).toEqual([]);
    // A prompt demanding no format at all is not warned about.
    expect(draftWarnings('Inspect the part.', null, 256)).toEqual([]);
  });
});

// ------------------------------------------------------------- Requirement 5.3

describe('Requirement 5.3: the preview of a saved Candidate', () => {
  it('shows the route\'s user message, system text and token budget verbatim', async () => {
    getTuningCandidatePreview.mockResolvedValue(
      preview({
        userMessage: 'ROUTE TEXT\n\nrendered by the builder',
        systemText: 'You are an inspector.',
        maxTokens: 512,
      })
    );
    await renderTab();
    await waitFor(() =>
      expect(screen.getByTestId('preview-user-message').textContent).toBe(
        'ROUTE TEXT\n\nrendered by the builder'
      )
    );
    expect(screen.getByTestId('preview-system-text').textContent).toBe(
      'You are an inspector.'
    );
    expect(screen.getByTestId('preview-max-tokens').textContent).toBe('512');
    expect(screen.queryByTestId('draft-preview-note')).toBeNull();
    expect(getTuningCandidatePreview).toHaveBeenCalledWith('cand-1', 'ts-1');
  });

  it('says no system text is sent when the route reports none', async () => {
    await renderTab();
    await waitFor(() =>
      expect(screen.getByTestId('preview-system-text').textContent).toBe(
        'No system text is sent.'
      )
    );
  });

  it('shows the route\'s warnings for the saved Candidate', async () => {
    getTuningCandidatePreview.mockResolvedValue(
      preview({
        warnings: [
          { code: 'max_tokens_truncation', message: 'route said: too few tokens' },
        ],
      })
    );
    await renderTab();
    await waitFor(() =>
      expect(
        screen.getByTestId('preview-warning-max_tokens_truncation').textContent
      ).toBe('route said: too few tokens')
    );
  });
});

describe('Requirement 5.3: the preview of an unsaved draft', () => {
  it('projects the edited prompt with the Verdict_Instruction and flags the draft', async () => {
    await renderTab();
    typeInto('candidate-prompt', 'Look for cracks.');
    await waitFor(() =>
      expect(screen.getByTestId('preview-user-message').textContent).toBe(
        `Look for cracks.\n\n${VERDICT_INSTRUCTION}`
      )
    );
    expect(screen.getByTestId('draft-preview-note').textContent).toBe(
      DRAFT_PREVIEW_NOTE
    );
  });

  it('projects the system text and the token budget of the draft', async () => {
    await renderTab();
    typeInto('candidate-system-prompt', '  You are strict.  ');
    typeInto('candidate-max-tokens', '128');
    await waitFor(() =>
      expect(screen.getByTestId('preview-system-text').textContent).toBe(
        '  You are strict.  '
      )
    );
    expect(screen.getByTestId('preview-max-tokens').textContent).toBe('128');
  });

  it('warns on the draft itself, before it is saved', async () => {
    await renderTab();
    typeInto('candidate-max-tokens', '16');
    await waitFor(() =>
      expect(
        screen.getByTestId('preview-warning-max_tokens_truncation').textContent
      ).toContain('max_tokens is 16')
    );
    typeInto('candidate-prompt', 'Answer as JSON with a verdict field.');
    await waitFor(() =>
      expect(
        screen.getByTestId('preview-warning-answer_schema_missing_is_anomalous')
      ).toBeTruthy()
    );
  });

  it('replaces the projection with the route\'s answer on save', async () => {
    await renderTab();
    typeInto('candidate-prompt', 'Look for cracks.');
    getTuningCandidatePreview.mockResolvedValue(
      preview({ userMessage: 'AUTHORITATIVE ANSWER' })
    );
    fireEvent.click(screen.getByTestId('save-candidate'));
    await waitFor(() =>
      expect(updateTuningCandidate).toHaveBeenCalledWith('ts-1', 'cand-1', {
        name: 'Candidate 1',
        prompt: 'Look for cracks.',
        systemPrompt: null,
        maxTokens: 512,
      })
    );
    await waitFor(() =>
      expect(screen.getByTestId('preview-user-message').textContent).toBe(
        'AUTHORITATIVE ANSWER'
      )
    );
    expect(screen.queryByTestId('draft-preview-note')).toBeNull();
  });

  it('refuses to save a max_tokens outside the node type\'s bounds', async () => {
    await renderTab();
    typeInto('candidate-max-tokens', '99999');
    await waitFor(() =>
      expect(screen.getByText('max_tokens must be at most 4096 for this node type')).toBeTruthy()
    );
    fireEvent.click(screen.getByTestId('save-candidate'));
    expect(updateTuningCandidate).not.toHaveBeenCalled();
  });
});

// -------------------------------------------------- Requirements 5.1 and 5.6

describe('Requirements 5.1, 5.6: the baseline and the starter template', () => {
  it('keeps the baseline read-only and offers a duplicate instead', async () => {
    await renderTab();
    fireEvent.click(screen.getByTestId('candidate-baseline'));
    await waitFor(() =>
      expect(screen.getByTestId('baseline-read-only').textContent).toBe(
        BASELINE_READ_ONLY_NOTE
      )
    );
    expect(
      screen.getByTestId('candidate-prompt').querySelector('textarea')
    ).toHaveProperty('readOnly', true);
    expect(screen.getByTestId('duplicate-baseline')).toBeTruthy();
    expect(screen.queryByTestId('delete-baseline')).toBeNull();
  });

  it('inserts the starter prompt only when the action is used', async () => {
    await renderTab();
    expect(
      screen.getByTestId('candidate-prompt').querySelector('textarea')!.value
    ).toBe('Compare the two images.');
    fireEvent.click(screen.getByTestId('insert-starter'));
    await waitFor(() =>
      expect(
        screen.getByTestId('candidate-prompt').querySelector('textarea')!.value
      ).toBe(STARTER_CANDIDATE.prompt)
    );
    expect(
      screen.getByTestId('candidate-max-tokens').querySelector('input')!.value
    ).toBe(`${STARTER_CANDIDATE.maxTokens}`);
    // The starter describes, compares and decides, and demands the verdict
    // JSON the parser needs, so it triggers no schema warning.
    expect(
      screen.queryByTestId('preview-warning-answer_schema_missing_is_anomalous')
    ).toBeNull();
  });

  it('shows the node and its model beside the editor', async () => {
    await renderTab();
    expect(
      screen.getByText(
        'Node bedrock_1 · bedrock_inference · anthropic.claude-3-5-sonnet'
      )
    ).toBeTruthy();
  });

  it('states that a VLM template is previewed unrendered', async () => {
    getTuningCandidatePreview.mockResolvedValue(
      preview({ nodeType: 'llm_inference', templateRendered: false })
    );
    await renderTab([candidate()], { ...NODE, nodeType: 'llm_inference' });
    await waitFor(() =>
      expect(screen.getByText('No (device renders it)')).toBeTruthy()
    );
    expect(screen.getByText('Prompt template')).toBeTruthy();
  });
});
