/**
 * Unit tests for the SyntheticData create-session wizard
 * (synthetic-defect-data-generation, task 7.6).
 *
 * - Catalog rendering with capability flags and empty-catalog guidance
 *   (Req 1.1, 1.3)
 * - Source classification flows: radio required, Defect_Type required for
 *   normal sources, optional for defect sources (Req 3.2, 3.3, 3.4)
 * - Variation count valid-range message (Req 4.4)
 * - Randomization controls shown per model capability flags with the
 *   model's defaults (Req 4.3)
 * - At-least-one-source validation (Req 3.6)
 */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import type { SyntheticModel } from './types';

const navigateMock = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock('../../contexts/UsecaseContext', () => ({
  UsecaseProvider: ({ children }: { children: ReactNode }) => children,
  useUsecase: () => ({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  }),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    listUseCases: vi.fn(),
    listSyntheticSessions: vi.fn(),
    listSyntheticModels: vi.fn(),
    listDatasets: vi.fn(),
    getImagePreview: vi.fn(),
    getSyntheticPromptTemplate: vi.fn(),
    putSyntheticPromptTemplate: vi.fn(),
    createSyntheticSession: vi.fn(),
  },
}));

import { apiService } from '../../services/api';
import SyntheticData, {
  NO_SOURCES_MESSAGE,
  SOURCE_PICKER_PAGE_SIZE,
  VARIATION_COUNT_MESSAGE,
  isValidVariationCount,
} from './SyntheticData';

const mocked = vi.mocked(apiService);

const NOVA: SyntheticModel = {
  model_id: 'amazon.nova-canvas-v1:0',
  display_name: 'Amazon Nova Canvas',
  capabilities: {
    text_to_image: true,
    inpainting: true,
    image_variation: true,
    seed: true,
    cfg_scale: true,
  },
  max_images_per_call: 1,
  randomization_defaults: { seed: null, cfg_scale: 6.5 },
};

/** A model without randomization capabilities (Req 4.3 negative case). */
const NO_RANDOMIZATION: SyntheticModel = {
  ...NOVA,
  model_id: 'example.fixed-model-v1:0',
  display_name: 'Fixed Example Model',
  capabilities: {
    text_to_image: true,
    inpainting: false,
    image_variation: true,
    seed: false,
    cfg_scale: false,
  },
};

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  if (!window.matchMedia) {
    (window as any).matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  mocked.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Castings' } as any],
    count: 1,
  });
  mocked.listSyntheticSessions.mockResolvedValue({ sessions: [], count: 0 });
  mocked.listSyntheticModels.mockResolvedValue({ models: [NOVA, NO_RANDOMIZATION] });
  mocked.listDatasets.mockResolvedValue({
    datasets: [
      {
        prefix: 'datasets/castings/',
        image_count: 12,
        last_modified: null,
        has_subdirectories: false,
      },
    ],
    bucket: 'bucket',
    base_prefix: '',
  });
  mocked.getImagePreview.mockResolvedValue({
    prefix: 'datasets/castings/',
    bucket: 'bucket',
    total_found: 2,
    offset: 0,
    limit: 12,
    has_more: false,
    images: [
      {
        key: 'datasets/castings/img1.png',
        filename: 'img1.png',
        size: 1,
        last_modified: '2025-01-01',
        presigned_url: 'https://example.com/img1.png',
      },
      {
        key: 'datasets/castings/img2.png',
        filename: 'img2.png',
        size: 1,
        last_modified: '2025-01-01',
        presigned_url: 'https://example.com/img2.png',
      },
    ],
    expires_in_seconds: 3600,
  });
  mocked.getSyntheticPromptTemplate.mockResolvedValue({
    template_text: 'A {object_type} with a {defect_type}',
    object_type: 'casting',
    defect_type: 'scratch',
    is_default: true,
  });
});

async function openWizard() {
  render(
    <MemoryRouter>
      <SyntheticData />
    </MemoryRouter>
  );
  const createButton = await screen.findByRole('button', {
    name: /create generation session/i,
  });
  await waitFor(() => expect(createButton).toBeEnabled());
  await userEvent.click(createButton);
  await waitFor(() => expect(mocked.listSyntheticModels).toHaveBeenCalled());
}

/**
 * 63-key paged fixture (source-image-picker-pagination, task 4): 32
 * anomaly-*.jpg sorting lexicographically before 31 normal-*.jpg, mirroring
 * the reported counterexample dataset.
 */
function pagedKeys(prefix: string): string[] {
  const anomaly = Array.from(
    { length: 32 },
    (_, i) => `${prefix}anomaly-${String(i).padStart(3, '0')}.jpg`
  );
  const normal = Array.from(
    { length: 31 },
    (_, i) => `${prefix}normal-${String(i).padStart(3, '0')}.jpg`
  );
  return [...anomaly, ...normal];
}

/** Make getImagePreview respect offset/limit and return paged slices. */
function mockPagedPreview() {
  mocked.getImagePreview.mockImplementation(async (params) => {
    const keys = pagedKeys(params.prefix);
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 8;
    const page = keys.slice(offset, offset + limit);
    return {
      prefix: params.prefix,
      bucket: 'bucket',
      total_found: keys.length,
      offset,
      limit,
      has_more: offset + limit < keys.length,
      images: page.map((key) => ({
        key,
        filename: key.slice(key.lastIndexOf('/') + 1),
        size: 1,
        last_modified: '2025-01-01',
        presigned_url: `https://example.com/${key}`,
      })),
      expires_in_seconds: 1800,
    };
  });
}

async function selectDataset(prefix: string) {
  // The FormField label "Dataset" is part of the trigger's accessible name
  // both before and after a selection.
  const datasetSelect = await screen.findByRole('button', {
    name: /^dataset\b/i,
  });
  await userEvent.click(datasetSelect);
  await userEvent.click(await screen.findByText(prefix));
}

async function selectModel(displayName: string) {
  // The FormField label is part of the trigger's accessible name both
  // before ("Generation model Select a generation model") and after a
  // selection ("Generation model <selected label>").
  const modelSelect = await screen.findByRole('button', {
    name: /generation model/i,
  });
  await userEvent.click(modelSelect);
  await userEvent.click(await screen.findByText(displayName));
}

describe('SyntheticData wizard', () => {
  it('renders the Model_Catalog with capability flags (Req 1.1)', async () => {
    await openWizard();
    await selectModel('Amazon Nova Canvas');
    // Selected model's capability flags render as badges.
    expect(await screen.findByText('inpainting')).toBeInTheDocument();
    expect(screen.getByText('seed')).toBeInTheDocument();
    expect(screen.getByText('cfg scale')).toBeInTheDocument();
  });

  it('shows the enabling-configuration guidance for an empty catalog (Req 1.3)', async () => {
    mocked.listSyntheticModels.mockResolvedValue({
      models: [],
      guidance:
        'No image generation models are available. Enable model access in the Amazon Bedrock console.',
    });
    await openWizard();
    expect(
      await screen.findByText(/enable model access in the amazon bedrock console/i)
    ).toBeInTheDocument();
  });

  it('requires a source classification and a Defect_Type for normal sources (Req 3.2, 3.3)', async () => {
    await openWizard();
    // Without a classification the validation list names the condition.
    expect(
      await screen.findByText(/must be classified as defect or normal images/i)
    ).toBeInTheDocument();

    // Classify as normal without a defect type -> the required message.
    await userEvent.click(screen.getByRole('radio', { name: /normal images/i }));
    expect(
      (
        await screen.findAllByText(
          /defect_type to synthesize is required for normal source images/i
        )
      ).length
    ).toBeGreaterThan(0);

    // Supplying the defect type clears the message (Req 3.3).
    await userEvent.type(screen.getByLabelText('Defect type'), 'scratch');
    await waitFor(() =>
      expect(
        screen.queryAllByText(
          /defect_type to synthesize is required for normal source images/i
        )
      ).toHaveLength(0)
    );
  });

  it('treats the Defect_Type as optional for defect sources (Req 3.4)', async () => {
    await openWizard();
    await userEvent.click(screen.getByRole('radio', { name: /defect images/i }));
    expect(
      screen.queryByText(
        /defect_type to synthesize is required for normal source images/i
      )
    ).not.toBeInTheDocument();
  });

  it('shows the valid-range message for an out-of-range Variation_Count (Req 4.4)', async () => {
    await openWizard();
    const input = screen.getByLabelText('Variation count');
    await userEvent.clear(input);
    await userEvent.type(input, '21');
    expect((await screen.findAllByText(VARIATION_COUNT_MESSAGE)).length).toBeGreaterThan(0);

    await userEvent.clear(input);
    await userEvent.type(input, '0');
    expect(screen.getAllByText(VARIATION_COUNT_MESSAGE).length).toBeGreaterThan(0);

    // A valid value clears the message.
    await userEvent.clear(input);
    await userEvent.type(input, '5');
    await waitFor(() =>
      expect(screen.queryAllByText(VARIATION_COUNT_MESSAGE)).toHaveLength(0)
    );
  });

  it('exposes seed/cfgScale controls per capability flags with model defaults (Req 4.3)', async () => {
    await openWizard();
    // No model selected: no randomization controls.
    expect(screen.queryByLabelText('Seed')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Guidance strength')).not.toBeInTheDocument();

    await selectModel('Amazon Nova Canvas');
    // Capabilities seed + cfg_scale: both controls appear with defaults.
    expect(await screen.findByLabelText('Seed')).toBeInTheDocument();
    const cfg = screen.getByLabelText('Guidance strength');
    expect(cfg).toHaveValue(6.5);

    // A model without those capabilities hides the controls.
    await selectModel('Fixed Example Model');
    await waitFor(() => {
      expect(screen.queryByLabelText('Seed')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Guidance strength')).not.toBeInTheDocument();
    });
  });

  it('rejects creation with zero Source_Images and the at-least-one message (Req 3.6)', async () => {
    await openWizard();
    expect(await screen.findByText(NO_SOURCES_MESSAGE)).toBeInTheDocument();
    const submit = screen.getByRole('button', { name: /^create session$/i });
    expect(submit).toBeDisabled();
    expect(mocked.createSyntheticSession).not.toHaveBeenCalled();
  });

  it('browses datasets and selects presigned Source_Image thumbnails (Req 3.1, 3.5)', async () => {
    await openWizard();
    const datasetSelect = await screen.findByRole('button', {
      name: /select a dataset prefix/i,
    });
    await userEvent.click(datasetSelect);
    await userEvent.click(await screen.findByText('datasets/castings/'));

    // Presigned thumbnails render (Req 3.5).
    const thumb = await screen.findByTestId('source-thumb-img1.png');
    expect(within(thumb).getByRole('img')).toHaveAttribute(
      'src',
      'https://example.com/img1.png'
    );

    // Selecting one clears the at-least-one-source validation (Req 3.6).
    await userEvent.click(thumb);
    await waitFor(() =>
      expect(screen.queryByText(NO_SOURCES_MESSAGE)).not.toBeInTheDocument()
    );
  });
});

describe('SyntheticData source-image picker pagination', () => {
  const PREFIX = 'datasets/castings/';

  beforeEach(() => {
    mockPagedPreview();
  });

  async function openPickerOnFirstPage() {
    await openWizard();
    await selectDataset(PREFIX);
    await screen.findByText('Showing 1–12 of 63');
  }

  function nextButton() {
    return screen.getByRole('button', { name: /next page/i });
  }

  function previousButton() {
    return screen.getByRole('button', { name: /previous page/i });
  }

  it('fetches pages with offsets 0, 12, 24 on Next navigation (Req 2.2)', async () => {
    await openPickerOnFirstPage();
    expect(mocked.getImagePreview).toHaveBeenCalledWith(
      expect.objectContaining({
        usecase_id: 'uc-1',
        prefix: PREFIX,
        offset: 0,
        limit: SOURCE_PICKER_PAGE_SIZE,
      })
    );
    // Page 1 renders the first slice.
    expect(
      await screen.findByTestId('source-thumb-anomaly-000.jpg')
    ).toBeInTheDocument();

    await userEvent.click(nextButton());
    await screen.findByText('Showing 13–24 of 63');
    expect(mocked.getImagePreview).toHaveBeenCalledWith(
      expect.objectContaining({ prefix: PREFIX, offset: 12 })
    );
    expect(
      screen.getByTestId('source-thumb-anomaly-012.jpg')
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('source-thumb-anomaly-000.jpg')
    ).not.toBeInTheDocument();

    await userEvent.click(nextButton());
    await screen.findByText('Showing 25–36 of 63');
    expect(mocked.getImagePreview).toHaveBeenCalledWith(
      expect.objectContaining({ prefix: PREFIX, offset: 24 })
    );
    // Page 3 crosses into the normal-*.jpg keys (offset 24..35).
    expect(
      screen.getByTestId('source-thumb-normal-000.jpg')
    ).toBeInTheDocument();

    // Previous returns to the prior page with the prior offset.
    await userEvent.click(previousButton());
    await screen.findByText('Showing 13–24 of 63');
    expect(mocked.getImagePreview).toHaveBeenLastCalledWith(
      expect.objectContaining({ prefix: PREFIX, offset: 12 })
    );
  });

  it('shows the position indicator and disables controls at the bounds (Req 2.2, 2.4)', async () => {
    await openPickerOnFirstPage();
    // First page: Previous disabled, Next enabled.
    expect(previousButton()).toBeDisabled();
    expect(nextButton()).toBeEnabled();

    await userEvent.click(nextButton());
    await screen.findByText('Showing 13–24 of 63');
    expect(previousButton()).toBeEnabled();

    // Walk to the last page (offsets 24, 36, 48, 60).
    for (const range of ['25–36', '37–48', '49–60', '61–63']) {
      await userEvent.click(nextButton());
      await screen.findByText(`Showing ${range} of 63`);
    }
    expect(nextButton()).toBeDisabled();
    expect(previousButton()).toBeEnabled();
  });

  it('preserves selections and the selected-count text across pages (Req 2.3)', async () => {
    await openPickerOnFirstPage();
    await userEvent.click(screen.getByTestId('source-thumb-anomaly-000.jpg'));
    expect(screen.getByText(/1 selected/)).toBeInTheDocument();

    await userEvent.click(nextButton());
    await screen.findByText('Showing 13–24 of 63');
    // Off-page selection still counted.
    expect(screen.getByText(/1 selected/)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId('source-thumb-anomaly-012.jpg'));
    expect(screen.getByText(/2 selected/)).toBeInTheDocument();

    // Back to page 1: selection intact, thumbnail still marked selected.
    await userEvent.click(previousButton());
    await screen.findByText('Showing 1–12 of 63');
    expect(screen.getByText(/2 selected/)).toBeInTheDocument();
    expect(
      screen.getByTestId('source-thumb-anomaly-000.jpg')
    ).toHaveAttribute('aria-pressed', 'true');
  });

  it('submits source_images containing keys selected on two pages (Req 2.5)', async () => {
    mocked.createSyntheticSession.mockResolvedValue({
      session: { session_id: 'sess-1' },
    } as any);
    await openPickerOnFirstPage();
    await selectModel('Amazon Nova Canvas');
    await userEvent.type(screen.getByLabelText('Object type'), 'casting');
    await userEvent.click(screen.getByRole('radio', { name: /defect images/i }));

    await userEvent.click(screen.getByTestId('source-thumb-anomaly-000.jpg'));
    await userEvent.click(nextButton());
    await screen.findByText('Showing 13–24 of 63');
    await userEvent.click(screen.getByTestId('source-thumb-anomaly-012.jpg'));

    const submit = screen.getByRole('button', { name: /^create session$/i });
    await waitFor(() => expect(submit).toBeEnabled());
    await userEvent.click(submit);

    await waitFor(() => expect(mocked.createSyntheticSession).toHaveBeenCalled());
    const payload = mocked.createSyntheticSession.mock.calls[0][0];
    const submittedKeys = (payload.source_images ?? []).map(
      (img: any) => img.key
    );
    expect(submittedKeys).toContain(`${PREFIX}anomaly-000.jpg`);
    expect(submittedKeys).toContain(`${PREFIX}anomaly-012.jpg`);
    expect(submittedKeys).toHaveLength(2);
  });

  it('resets the page and clears the selection on dataset change (Req 3.6)', async () => {
    mocked.listDatasets.mockResolvedValue({
      datasets: [
        {
          prefix: PREFIX,
          image_count: 63,
          last_modified: null,
          has_subdirectories: false,
        },
        {
          prefix: 'datasets/widgets/',
          image_count: 63,
          last_modified: null,
          has_subdirectories: false,
        },
      ],
      bucket: 'bucket',
      base_prefix: '',
    });
    await openPickerOnFirstPage();
    await userEvent.click(screen.getByTestId('source-thumb-anomaly-000.jpg'));
    await userEvent.click(nextButton());
    await screen.findByText('Showing 13–24 of 63');
    expect(screen.getByText(/1 selected/)).toBeInTheDocument();

    // Change dataset: page resets to offset 0 and the selection is cleared.
    await selectDataset('datasets/widgets/');
    await screen.findByText('Showing 1–12 of 63');
    expect(mocked.getImagePreview).toHaveBeenLastCalledWith(
      expect.objectContaining({ prefix: 'datasets/widgets/', offset: 0 })
    );
    expect(screen.getByText(/0 selected/)).toBeInTheDocument();
    // At-least-one-source validation returns (Req 3.6).
    expect(screen.getByText(NO_SOURCES_MESSAGE)).toBeInTheDocument();
  });
});

describe('isValidVariationCount', () => {
  it('accepts exactly integers 1..20 (Req 4.1)', () => {
    expect(isValidVariationCount('1')).toBe(true);
    expect(isValidVariationCount('20')).toBe(true);
    expect(isValidVariationCount('0')).toBe(false);
    expect(isValidVariationCount('21')).toBe(false);
    expect(isValidVariationCount('2.5')).toBe(false);
    expect(isValidVariationCount('-3')).toBe(false);
    expect(isValidVariationCount('abc')).toBe(false);
    expect(isValidVariationCount('')).toBe(false);
  });
});
