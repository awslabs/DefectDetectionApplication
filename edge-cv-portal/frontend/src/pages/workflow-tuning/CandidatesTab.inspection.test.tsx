/**
 * The Candidates tab for an inspection-format node (AC-7 frontend, FR-5.1).
 *
 * An inspection-format node answers the inspection JSON, so the editor
 * offers no anomaly-mode starter template, projects an unsaved draft with
 * the inspection instruction and warns when a JSON answer format omits
 * `verdict`. With `render_prompt` on it says that the template is rendered
 * per run, and the preview's "Template rendered" reads "No (rendered per
 * sample when scored)". The request's attachments are listed under the user
 * message, so the author sees the labels the prompt must use.
 *
 * At c299a6e1 the `insert-starter` button is present, "Template rendered"
 * reads "No (device renders it)", and no attachment list is shown.
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
  within,
} from '@testing-library/react';
import CandidatesTab from './CandidatesTab';
import {
  INSTRUCTION_SEPARATOR,
  VERDICT_INSTRUCTION,
  draftWarnings,
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

/** Restated from the shared module (the fixture pins the frontend copy). */
const INSPECTION_INSTRUCTION_TEXT =
  'Respond with exactly one JSON object and nothing else: {"verdict": '
  + '"pass"|"fail", "confidence": 0..1, "reason": "<one sentence>", '
  + '"objects": [{"name": "<part>", "qc": "OK"|"NOK", "reason": "<short>", '
  + '"bounding_box": {"x_min": <int>, "y_min": <int>, "x_max": <int>, '
  + '"y_max": <int>}}, ...]} with one entry in objects per expected part.';

const RENDER_DESCRIPTION =
  'Placeholders are rendered per run against the trigger payload and the '
  + 'image size; the preview shows the template unrendered.';

const INSPECTION_NODE: TuningNodeView = {
  nodeId: 'check',
  nodeType: 'bedrock_inference',
  model: 'us.amazon.nova-pro-v1:0',
  parameters: {
    answer_format: 'inspection',
    render_prompt: true,
    reference_payload_path:
      'file:///aws_dda/reference/legend.jpg\nfile:///aws_dda/reference/notes.txt',
  },
  promptSet: { prompt: 'Check the parts.', systemPrompt: null, maxTokens: 600 },
  maxTokensBounds: { min: 1, max: 10000 },
};

const LEGACY_NODE: TuningNodeView = {
  ...INSPECTION_NODE,
  parameters: { anomaly_mode: true },
};

const CANDIDATE: TuningCandidate = {
  candidateId: 'cand-1',
  name: 'Candidate 1',
  prompt: 'Check order {trigger.payload_json.order_id}.',
  systemPrompt: null,
  maxTokens: 600,
  isBaseline: false,
  latestRun: null,
};

function inspectionPreview(): CandidatePreviewResponse {
  return {
    candidateId: 'cand-1',
    sessionId: 'ts-1',
    nodeType: 'bedrock_inference',
    userMessage: `${CANDIDATE.prompt}\n\n${INSPECTION_INSTRUCTION_TEXT}`,
    systemText: null,
    maxTokens: 600,
    model: 'us.amazon.nova-pro-v1:0',
    templateRendered: false,
    attachments: [
      { label: 'Input image', kind: 'image', format: 'jpeg' },
      { label: 'Reference 1', kind: 'image', format: 'jpeg' },
      { label: 'Reference 2', kind: 'document', format: 'txt' },
    ],
    warnings: [],
  };
}

async function renderTab(node: TuningNodeView) {
  render(
    <CandidatesTab
      sessionId="ts-1"
      node={node}
      candidates={[CANDIDATE]}
      onChanged={vi.fn()}
    />
  );
  await waitFor(() => expect(getTuningCandidatePreview).toHaveBeenCalled());
}

function typeInto(testId: string, value: string) {
  const field = screen.getByTestId(testId);
  const input =
    field.querySelector('textarea') ?? (field.querySelector('input') as HTMLElement);
  fireEvent.change(input, { target: { value } });
}

beforeEach(() => {
  vi.clearAllMocks();
  getTuningCandidatePreview.mockResolvedValue(inspectionPreview());
});

afterEach(() => {
  cleanup();
});

describe('an inspection-format node in the Candidates tab', () => {
  it('offers no starter template', async () => {
    await renderTab(INSPECTION_NODE);
    expect(screen.queryByTestId('insert-starter')).toBeNull();
  });

  it('states that the template is rendered per sample when scored', async () => {
    await renderTab(INSPECTION_NODE);
    await waitFor(() =>
      expect(screen.getByText('No (rendered per sample when scored)')).toBeTruthy()
    );
    expect(screen.getByText(RENDER_DESCRIPTION)).toBeTruthy();
    expect(screen.getByTestId('preview-user-message').textContent).toBe(
      inspectionPreview().userMessage
    );
  });

  it('lists the attachments in request order', async () => {
    await renderTab(INSPECTION_NODE);
    const list = await waitFor(() => screen.getByTestId('preview-attachments'));
    expect(within(list).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Input image · image · JPEG',
      'Reference 1 · image · JPEG',
      'Reference 2 · document · TXT',
    ]);
  });

  it('projects an unsaved draft with the inspection instruction and its warning', async () => {
    await renderTab(INSPECTION_NODE);
    typeInto('candidate-prompt', 'Answer in JSON.');
    await waitFor(() =>
      expect(screen.getByTestId('preview-user-message').textContent).toBe(
        `Answer in JSON.${INSTRUCTION_SEPARATOR}${INSPECTION_INSTRUCTION_TEXT}`
      )
    );
    expect(
      screen.getByTestId('preview-warning-answer_schema_missing_verdict')
    ).toBeTruthy();
    expect(
      screen.queryByTestId('preview-warning-answer_schema_missing_is_anomalous')
    ).toBeNull();
  });

  it('draftWarnings names verdict for an inspection-format node', () => {
    const warnings = draftWarnings('Answer in JSON.', 'Reply {"ok": true}', 32, 'inspection');
    expect(warnings.map((w) => [w.code, w.field ?? null])).toEqual([
      ['max_tokens_truncation', null],
      ['answer_schema_missing_verdict', 'prompt'],
      ['answer_schema_missing_verdict', 'systemPrompt'],
    ]);
    expect(warnings[1].message).toBe(
      'The prompt specifies an answer format that does not include "verdict". '
        + "An inspection-format node's parser requires exactly one JSON object "
        + 'with verdict, confidence, reason and objects.'
    );
    expect(
      draftWarnings('Answer {"verdict": "pass"} in JSON.', null, 600, 'inspection')
    ).toEqual([]);
  });
});

describe('a legacy anomaly-mode node in the Candidates tab', () => {
  it('is unchanged (preservation: passes at c299a6e1 too)', async () => {
    getTuningCandidatePreview.mockResolvedValue({
      ...inspectionPreview(),
      userMessage: `${CANDIDATE.prompt}\n\n${VERDICT_INSTRUCTION}`,
      templateRendered: true,
      attachments: undefined,
    });
    await renderTab(LEGACY_NODE);
    expect(screen.getByTestId('insert-starter')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('Yes')).toBeTruthy());
    expect(screen.queryByTestId('preview-attachments')).toBeNull();
    expect(screen.queryByText(RENDER_DESCRIPTION)).toBeNull();
    typeInto('candidate-prompt', 'Answer in JSON.');
    await waitFor(() =>
      expect(screen.getByTestId('preview-user-message').textContent).toBe(
        `Answer in JSON.${INSTRUCTION_SEPARATOR}${VERDICT_INSTRUCTION}`
      )
    );
    expect(
      screen.getByTestId('preview-warning-answer_schema_missing_is_anomalous')
    ).toBeTruthy();
  });
});
