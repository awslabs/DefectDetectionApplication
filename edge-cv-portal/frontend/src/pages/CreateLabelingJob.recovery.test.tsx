/**
 * Vitest example tests for the labeling wizard's session-recovery flows
 * (labeling-setup-session-recovery task 3.2, Requirements 1.1, 1.2, 1.3,
 * 1.5, 1.6, 2.3, 3.1, 3.4, 3.5, 3.6, 3.7, 3.8, 4.1, 4.2, 4.5, 5.5, 5.6,
 * 6.1, 6.4, 7.3, 7.4).
 *
 * Covers, by example:
 * - a burst of edits produces one debounced Setup_Draft write carrying the
 *   edited values (1.1);
 * - a pristine mount writes nothing (1.2);
 * - an unresolved Restore_Offer suppresses writes — new input cannot
 *   clobber the draft being offered (1.3);
 * - a throwing Draft_Store write leaves the wizard operating (1.5);
 * - the written draft JSON carries no idToken key or bearer token value
 *   (1.6);
 * - the offer presents exactly Restore and Discard, at most once per use
 *   case per mount (3.1);
 * - a restored team id is re-selected once the team list loads, and an
 *   absent id leaves the team unselected (3.4);
 * - the model selection restores verbatim when the capability-filtered
 *   picker omits it (3.5);
 * - a non-admin restore drops the Skip_Verification_Configuration (3.6);
 * - Discard clears the key and touches no wizard state (3.7);
 * - clean storage shows no offer and the unchanged wizard (3.8, 7.4);
 * - restored refs render as basename-named, removable chips whose removal
 *   reflects in the next write (4.1, 2.3);
 * - restored refs count with new files toward the per-designation limit
 *   message (4.2);
 * - a use-case switch discards restored refs (4.5);
 * - an out-of-window Preview_Run_Reference is dropped silently at restore
 *   (5.5);
 * - a newly started run replaces `previewRun` in the next write (5.6);
 * - successful creation removes the key before navigating (6.1);
 * - unmounting after edits keeps the key (6.4);
 * - a restored-invalid draft surfaces the standard validation message
 *   (7.3).
 *
 * Mocking and wizard navigation follow the `CreateLabelingJob.test.tsx` /
 * `CreateLabelingJob.modelpicker.test.tsx` scaffolding. Timing uses real
 * timers: positive write assertions poll with `waitFor` timeouts above
 * `DRAFT_SAVE_DEBOUNCE_MS`, and negative assertions wait out the debounce
 * inside `act` before inspecting storage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateLabelingJob from './CreateLabelingJob';
import { DRAFT_SAVE_DEBOUNCE_MS, labelingJobDraftKey } from './labelingJobDraft';
import type { LabelingJobDraft } from './labelingJobDraft';

const { apiMocks, navigateMock } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingTeams: vi.fn(),
    getBedrockModels: vi.fn(),
    createLabelingJob: vi.fn(),
    listWorkteams: vi.fn(),
    getImagePreview: vi.fn(),
    startPreviewRun: vi.fn(),
    getPreviewRun: vi.fn(),
  },
  navigateMock: vi.fn(),
}));

vi.mock('../services/api', () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
  }
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      // Any other API call the page happens to make resolves to an empty
      // object so effects settle without error.
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

vi.mock('../contexts/UsecaseContext', () => ({
  useUsecase: () => ({
    selectedUsecaseId: 'uc-1',
    setSelectedUsecaseId: vi.fn(),
  }),
}));

// A non-admin user: Requirement 3.6's restore-side drop of the
// Skip_Verification_Configuration is the behavior under test.
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'user', role: 'DataScientist' },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useLocation: () => ({ state: undefined }),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

vi.mock('../components/S3Browser', () => ({ default: () => null }));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** Image_Capable catalog model offered by the Auto_Label_Picker. */
const NOVA = {
  id: 'us.amazon.nova-pro-v1:0',
  label: 'Nova Pro',
  image_input: true,
};
/** Text_Only model — the capability-filtered picker omits it (Req 3.5). */
const TITAN_TEXT = {
  id: 'amazon.titan-text-express-v1',
  label: 'Titan Text Express',
  image_input: false,
};

const USE_CASES = {
  usecases: [
    { usecase_id: 'uc-1', name: 'UC1', s3_bucket: 'out-bucket' },
    { usecase_id: 'uc-2', name: 'UC2', s3_bucket: 'out-bucket-2' },
  ],
  count: 2,
};

const DRAFT_KEY = labelingJobDraftKey('uc-1');

const emptyListing = {
  prefix: 'images/',
  bucket: 'data-bucket',
  total_found: 0,
  offset: 0,
  limit: 50,
  has_more: false,
  images: [],
  expires_in_seconds: 900,
};

const GOOD_REF_1 = 's3://data-bucket/labeling-examples/j1/good/good-one.png';
const GOOD_REF_2 = 's3://data-bucket/labeling-examples/j1/good/good-two.png';
const BAD_REF_1 = 's3://data-bucket/labeling-examples/j1/bad/bad-one.png';

const pngFile = (name: string) =>
  new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

/**
 * A conforming Setup_Draft a previous session would have written: DDA
 * branch on the labeling setup step, team t-1, one label, auto-label off.
 * Tests override the fields their scenario varies.
 */
function makeDraft(overrides: Partial<LabelingJobDraft> = {}): LabelingJobDraft {
  return {
    version: 1,
    savedAtMs: Date.now(),
    usecaseId: 'uc-1',
    activeStepIndex: 4,
    labelingBackend: 'DDA',
    jobName: 'draft-job',
    description: 'saved mid-setup',
    datasetS3Uri: 's3://bucket/images/',
    maskPrefix: '',
    taskTypeValue: 'Segmentation',
    workforceTypeValue: 'private',
    labelCategories: '',
    gtInstructions: '',
    enableAutomatedLabeling: false,
    ddaLabels: ['scratch'],
    ddaInstructions: '',
    selectedTeam: { teamId: 't-1', teamName: 'Team One' },
    autoLabelEnabled: false,
    autoLabelModel: '',
    detectionPrompt: '',
    fewShotEnabled: false,
    downscaleMaxEdge: null,
    tokenBudget: '',
    skipVerification: false,
    skipVerificationModelId: '',
    perLabelPrompts: {},
    exampleRefs: { good: [], bad: [] },
    previewSelectedKeys: [],
    previewRun: null,
    ...overrides,
  };
}

function seedDraft(draft: LabelingJobDraft): string {
  const raw = JSON.stringify(draft);
  window.localStorage.setItem(labelingJobDraftKey(draft.usecaseId), raw);
  return raw;
}

const storedDraftRaw = () => window.localStorage.getItem(DRAFT_KEY);
const storedDraft = (): LabelingJobDraft | null => {
  const raw = storedDraftRaw();
  return raw === null ? null : (JSON.parse(raw) as LabelingJobDraft);
};

/** Wait out the debounce window plus slack, flushing settled effects. */
const settlePastDebounce = () =>
  act(async () => {
    await new Promise((resolve) =>
      setTimeout(resolve, DRAFT_SAVE_DEBOUNCE_MS + 450)
    );
  });

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
  apiMocks.listUseCases.mockResolvedValue(USE_CASES);
  apiMocks.listLabelingTeams.mockResolvedValue({
    teams: [{ team_id: 't-1', team_name: 'Team One', members: ['a'] }],
    count: 1,
  });
  apiMocks.getBedrockModels.mockResolvedValue({
    models: [NOVA],
    region: 'us-east-1',
  });
  apiMocks.listWorkteams.mockResolvedValue({ workteams: [] });
  apiMocks.createLabelingJob.mockResolvedValue({});
  apiMocks.getImagePreview.mockResolvedValue(emptyListing);
  apiMocks.startPreviewRun.mockResolvedValue({
    run_id: 'run-x',
    sample_count: 1,
    status: 'Running',
  });
  apiMocks.getPreviewRun.mockResolvedValue({
    run_id: 'run-x',
    status: 'Completed',
    sample_count: 0,
    few_shot: { enabled: false, attached: 0, omitted: 0 },
    results: [],
  });
  // Cloudscape's file thumbnails go through object URLs, which jsdom lacks.
  if (!URL.createObjectURL) {
    URL.createObjectURL = () => 'blob:example';
    URL.revokeObjectURL = () => undefined;
  }
});

// Storage.prototype spies (the write-throw and write-count tests) must not
// leak their implementations into later tests.
afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const clickNext = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
const clickPrevious = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Previous' }));

/** Step 0: choose DDA; step 1: type the job name — a Draft_Worthy edit. */
async function selectDdaAndTypeJobName(container: HTMLElement, name: string) {
  fireEvent.click(
    createWrapper(container).findRadioGroup()!.findInputByValue('DDA')!.getElement()
  );
  clickNext();
  const nameInput = await screen.findByPlaceholderText(
    'e.g., Defect Detection - Batch 1'
  );
  fireEvent.change(nameInput, { target: { value: name } });
  return nameInput as HTMLInputElement;
}

/** Seed a draft, mount the wizard, and wait for the Restore_Offer. */
async function mountWithOffer(draft: LabelingJobDraft) {
  seedDraft(draft);
  const view = render(<CreateLabelingJob />);
  await screen.findByTestId('draft-restore-offer');
  return view;
}

/** Resolve the offer by restoring and wait for it to leave the page. */
async function restoreDraft() {
  fireEvent.click(screen.getByTestId('draft-restore-button'));
  await waitFor(() =>
    expect(screen.queryByTestId('draft-restore-offer')).not.toBeInTheDocument()
  );
}

/** The labeling-team select: the first select on the DDA setup step. */
const teamSelect = (container: HTMLElement) =>
  createWrapper(container).findAllSelects()[0];

const teamTriggerText = (container: HTMLElement) =>
  teamSelect(container).findTrigger().getElement().textContent || '';

// ---------------------------------------------------------------------------
// Continuous draft capture (Req 1.1, 1.2, 1.3, 1.5, 1.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob recovery — continuous draft capture', () => {
  it('writes one debounced draft carrying the edited value after a burst of edits (Req 1.1)', async () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    const { container } = render(<CreateLabelingJob />);

    // A burst of Job_Creator input: backend choice, step advance, and a
    // job name typed in three keystroke bursts.
    fireEvent.click(
      createWrapper(container).findRadioGroup()!.findInputByValue('DDA')!.getElement()
    );
    clickNext();
    const nameInput = await screen.findByPlaceholderText(
      'e.g., Defect Detection - Batch 1'
    );
    fireEvent.change(nameInput, { target: { value: 'r' } });
    fireEvent.change(nameInput, { target: { value: 'recovered' } });
    fireEvent.change(nameInput, { target: { value: 'recovered-job' } });

    await waitFor(
      () => expect(storedDraft()?.jobName).toBe('recovered-job'),
      { timeout: 4000 }
    );
    await settlePastDebounce();

    // The whole burst produced exactly one write under the Draft_Key.
    const draftWrites = setItemSpy.mock.calls.filter(
      ([key]) => key === DRAFT_KEY
    );
    expect(draftWrites).toHaveLength(1);

    const draft = storedDraft()!;
    expect(draft.version).toBe(1);
    expect(draft.usecaseId).toBe('uc-1');
    expect(draft.labelingBackend).toBe('DDA');
    expect(draft.jobName).toBe('recovered-job');
    expect(draft.activeStepIndex).toBe(1);
    expect(typeof draft.savedAtMs).toBe('number');
  });

  it('writes nothing on a pristine mount (Req 1.2)', async () => {
    render(<CreateLabelingJob />);
    // The use case resolves and every mount effect settles...
    await waitFor(() => expect(apiMocks.listUseCases).toHaveBeenCalled());
    // ...and well past the debounce, merely visiting the page has created
    // no draft under any key.
    await settlePastDebounce();
    expect(storedDraftRaw()).toBeNull();
    expect(window.localStorage.length).toBe(0);
  });

  it('suppresses draft writes while the Restore_Offer is unresolved (Req 1.3)', async () => {
    const seededRaw = seedDraft(makeDraft({ jobName: 'offered-draft' }));
    const { container } = render(<CreateLabelingJob />);
    await screen.findByTestId('draft-restore-offer');

    // New input before the offer is resolved: backend choice, step
    // advance, typing — none of it may overwrite the offered draft.
    await selectDdaAndTypeJobName(container, 'typed-while-offered');
    await settlePastDebounce();

    expect(storedDraftRaw()).toBe(seededRaw);
    // The offer itself is still standing, unresolved.
    expect(screen.getByTestId('draft-restore-offer')).toBeInTheDocument();
  });

  it('keeps operating when the Draft_Store write throws (Req 1.5)', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded');
    });
    const { container } = render(<CreateLabelingJob />);

    const nameInput = await selectDdaAndTypeJobName(
      container,
      'still-typing-works'
    );
    await settlePastDebounce();

    // Typing kept working and no error surfaced; only persistence is gone.
    expect(nameInput.value).toBe('still-typing-works');
    expect(storedDraftRaw()).toBeNull();
    expect(
      screen.queryByText("Couldn't create the labeling job")
    ).not.toBeInTheDocument();
  });

  it('writes no idToken key or bearer token value into the draft JSON (Req 1.6)', async () => {
    // AuthContext persists the Cognito id token under this key
    // (AuthContext.tsx: localStorage.setItem('idToken', ...)).
    const TOKEN_VALUE = 'test-bearer-token-abc123';
    window.localStorage.setItem('idToken', TOKEN_VALUE);
    const { container } = render(<CreateLabelingJob />);

    await selectDdaAndTypeJobName(container, 'token-free-job');
    await waitFor(
      () => expect(storedDraft()?.jobName).toBe('token-free-job'),
      { timeout: 4000 }
    );

    const raw = storedDraftRaw()!;
    expect(raw).not.toContain('idToken');
    expect(raw).not.toContain(TOKEN_VALUE);
  });
});

// ---------------------------------------------------------------------------
// Restore offer (Req 3.1, 3.7, 3.8, 7.4)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob recovery — restore offer', () => {
  it('shows the offer with exactly Restore and Discard, at most once per use case per mount (Req 3.1)', async () => {
    await mountWithOffer(makeDraft());

    // Exactly the two actions, both present, on a non-dismissible offer.
    const offer = screen.getByTestId('draft-restore-offer');
    expect(screen.getByTestId('draft-restore-button')).toBeInTheDocument();
    expect(screen.getByTestId('draft-discard-button')).toBeInTheDocument();
    expect(within(offer).getAllByRole('button')).toHaveLength(2);

    // Resolve the offer, re-seed a draft, and force re-renders: the draft
    // is read at most once per use case per mount, so no second offer.
    fireEvent.click(screen.getByTestId('draft-discard-button'));
    await waitFor(() =>
      expect(
        screen.queryByTestId('draft-restore-offer')
      ).not.toBeInTheDocument()
    );
    seedDraft(makeDraft());
    fireEvent.click(
      createWrapper(document.body).findRadioGroup()!.findInputByValue('DDA')!.getElement()
    );
    await settlePastDebounce();
    expect(
      screen.queryByTestId('draft-restore-offer')
    ).not.toBeInTheDocument();
  });

  it('Discard removes the stored draft and touches no wizard state (Req 3.7)', async () => {
    const { container } = await mountWithOffer(makeDraft());

    fireEvent.click(screen.getByTestId('draft-discard-button'));
    await waitFor(() =>
      expect(
        screen.queryByTestId('draft-restore-offer')
      ).not.toBeInTheDocument()
    );

    // The key is gone from the Draft_Store...
    expect(storedDraftRaw()).toBeNull();
    // ...and the wizard state is untouched: still on the first step with
    // no backend selected, none of the draft's values applied.
    const ddaRadio = createWrapper(container)
      .findRadioGroup()!
      .findInputByValue('DDA')!
      .getElement() as HTMLInputElement;
    expect(ddaRadio.checked).toBe(false);
    expect(screen.getByText('Choose how this job is executed')).toBeInTheDocument();
  });

  it('renders no offer and the unchanged wizard when storage is clean (Req 3.8, 7.4)', async () => {
    render(<CreateLabelingJob />);
    await waitFor(() => expect(apiMocks.listUseCases).toHaveBeenCalled());
    await settlePastDebounce();

    expect(
      screen.queryByTestId('draft-restore-offer')
    ).not.toBeInTheDocument();
    // The wizard renders exactly its usual first step.
    expect(screen.getByText('Labeling backend')).toBeInTheDocument();
    expect(screen.getByText('DDA Data Labeling System')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Restore application (Req 3.4, 3.5, 3.6, 7.3)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob recovery — restore application', () => {
  it('re-selects the drafted team once the team list has loaded (Req 3.4)', async () => {
    const { container } = await mountWithOffer(
      makeDraft({ selectedTeam: { teamId: 't-1', teamName: 'Team One' } })
    );
    await restoreDraft();
    await screen.findByText('Model-assisted pre-labeling');

    await waitFor(() =>
      expect(teamTriggerText(container)).toContain('Team One')
    );
  });

  it('leaves the team unselected when the drafted team id is gone from the list (Req 3.4)', async () => {
    const { container } = await mountWithOffer(
      makeDraft({ selectedTeam: { teamId: 't-gone', teamName: 'Ghost Team' } })
    );
    await restoreDraft();
    await screen.findByText('Model-assisted pre-labeling');

    // The team list settles without the drafted id: placeholder, not the
    // drafted name.
    await waitFor(() =>
      expect(teamTriggerText(container)).toContain('Select a labeling team')
    );
    expect(teamTriggerText(container)).not.toContain('Ghost Team');
  });

  it('restores the model selection verbatim when the capability-filtered picker omits it (Req 3.5)', async () => {
    // The catalog positively marks the drafted model text-only, so the
    // Auto_Label_Picker offers no entry for it.
    apiMocks.getBedrockModels.mockResolvedValue({
      models: [TITAN_TEXT],
      region: 'us-east-1',
    });
    await mountWithOffer(
      makeDraft({
        activeStepIndex: 5,
        autoLabelEnabled: true,
        autoLabelModel: `llm:${TITAN_TEXT.id}`,
        detectionPrompt: 'find hairline cracks',
        tokenBudget: '9000',
      })
    );
    await restoreDraft();

    // The review step presents the restored value verbatim...
    expect(
      await screen.findByText(`Prompt-guided: ${TITAN_TEXT.id}`)
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Team One')).toBeInTheDocument());

    // ...and the submitted payload carries it verbatim, with the draft's
    // prompt character-for-character and the draft's token budget rather
    // than the model-change pre-fill (Req 3.3 defusal observed here).
    fireEvent.click(screen.getByRole('button', { name: 'Create Job' }));
    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
    );
    const payload = apiMocks.createLabelingJob.mock.calls[0][0];
    expect(payload.auto_label).toEqual({
      enabled: true,
      model: `llm:${TITAN_TEXT.id}`,
      detection_prompt: 'find hairline cracks',
      downscale_max_edge: null,
      token_budget: 9000,
    });
  });

  it('drops skip-verification when a non-admin restores a draft with it enabled (Req 3.6)', async () => {
    await mountWithOffer(
      makeDraft({
        activeStepIndex: 5,
        skipVerification: true,
        skipVerificationModelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
        perLabelPrompts: { scratch: 'is there a scratch?' },
      })
    );
    await restoreDraft();

    // The review step's team row shows the team, not the
    // skip-verification "Not required" text: skip verification restored
    // disabled for the non-admin.
    await waitFor(() => expect(screen.getByText('Team One')).toBeInTheDocument());
    expect(
      screen.queryByText('Not required (skip verification)')
    ).not.toBeInTheDocument();

    // The submission carries the team path and none of the
    // skip-verification fields.
    fireEvent.click(screen.getByRole('button', { name: 'Create Job' }));
    await waitFor(() =>
      expect(apiMocks.createLabelingJob).toHaveBeenCalledTimes(1)
    );
    const payload = apiMocks.createLabelingJob.mock.calls[0][0];
    expect(payload.team_id).toBe('t-1');
    expect(payload).not.toHaveProperty('skip_verification');
    expect(payload).not.toHaveProperty('bedrock_model_id');
    expect(payload).not.toHaveProperty('per_label_prompts');
  });

  it('surfaces the standard validation message for a restored draft with an invalid setup (Req 7.3)', async () => {
    const { container } = await mountWithOffer(
      makeDraft({ selectedTeam: null })
    );
    await restoreDraft();
    await screen.findByText('Model-assisted pre-labeling');
    // Let the team list settle: no team was drafted, so none is selected.
    await waitFor(() =>
      expect(teamTriggerText(container)).toContain('Select a labeling team')
    );

    // Next-step on the restored-invalid setup: the existing message, and
    // the wizard stays on the setup step.
    clickNext();
    expect(
      await screen.findByText('A labeling team is required')
    ).toBeInTheDocument();
    expect(screen.getByText('Model-assisted pre-labeling')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Restored example references (Req 4.1, 4.2, 4.5, 2.3)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob recovery — restored example references', () => {
  it('shows restored refs as basename-named chips whose removal reflects in the next write (Req 4.1, 2.3)', async () => {
    await mountWithOffer(
      makeDraft({
        exampleRefs: { good: [GOOD_REF_1, GOOD_REF_2], bad: [BAD_REF_1] },
      })
    );
    await restoreDraft();
    await screen.findByText('Model-assisted pre-labeling');

    // One chip per restored ref, named by the ref's basename.
    const chips = screen.getAllByTestId('restored-example-chip');
    expect(chips).toHaveLength(3);
    expect(chips.map((chip) => chip.textContent)).toEqual([
      'good-one.png',
      'good-two.png',
      'bad-one.png',
    ]);

    // Each chip is individually removable; the removal reaches the next
    // draft write's Merged_Example_Refs.
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Remove restored example good-one.png',
      })
    );
    expect(screen.getAllByTestId('restored-example-chip')).toHaveLength(2);
    await waitFor(
      () =>
        expect(storedDraft()?.exampleRefs).toEqual({
          good: [GOOD_REF_2],
          bad: [BAD_REF_1],
        }),
      { timeout: 4000 }
    );
  });

  it('reports the combined-count limit when restored refs plus new files exceed the per-designation cap (Req 4.2)', async () => {
    const tenGoodRefs = Array.from(
      { length: 10 },
      (_, i) => `s3://data-bucket/labeling-examples/j1/good/g-${i}.png`
    );
    const { container } = await mountWithOffer(
      makeDraft({ exampleRefs: { good: tenGoodRefs, bad: [] } })
    );
    await restoreDraft();
    await screen.findByText('Model-assisted pre-labeling');

    // Ten restored refs alone sit exactly at the limit: no message.
    expect(screen.getAllByTestId('restored-example-chip')).toHaveLength(10);
    expect(
      screen.queryByText('At most 10 good example images are allowed')
    ).not.toBeInTheDocument();

    // One newly staged file tips the combined count to eleven.
    const goodUpload = createWrapper(container).findAllFileUploads()[0];
    fireEvent.change(goodUpload.findNativeInput().getElement(), {
      target: { files: [pngFile('extra.png')] },
    });
    expect(
      (await screen.findAllByText('At most 10 good example images are allowed'))
        .length
    ).toBeGreaterThan(0);
  });

  it('discards restored refs when the selected use case changes (Req 4.5)', async () => {
    const { container } = await mountWithOffer(
      makeDraft({ exampleRefs: { good: [GOOD_REF_1], bad: [] } })
    );
    await restoreDraft();
    await screen.findByText('Model-assisted pre-labeling');
    expect(screen.getAllByTestId('restored-example-chip')).toHaveLength(1);

    // Back to the job-configuration step and over to the other use case.
    clickPrevious();
    clickPrevious();
    clickPrevious();
    const useCaseSelect = createWrapper(container).findSelect()!;
    useCaseSelect.openDropdown();
    useCaseSelect.selectOptionByValue('uc-2');
    await waitFor(() =>
      expect(useCaseSelect.findTrigger().getElement().textContent).toContain(
        'UC2'
      )
    );

    // Forward to the setup step again: the restored refs are gone — they
    // never cross into another use case's data bucket scope.
    clickNext();
    clickNext();
    clickNext();
    await screen.findByText('Model-assisted pre-labeling');
    expect(screen.queryAllByTestId('restored-example-chip')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Preview run reference (Req 5.5, 5.6)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob recovery — preview run reference', () => {
  it('drops an out-of-window preview reference silently at restore (Req 5.5)', async () => {
    // For 1 sample the Resume_Window is (min(1×120+60, 900) + 3600) s =
    // 3780 s; a run started ~2.8 hours ago lies far outside it.
    await mountWithOffer(
      makeDraft({
        autoLabelEnabled: true,
        autoLabelModel: `llm:${NOVA.id}`,
        detectionPrompt: 'find scratches',
        tokenBudget: '9000',
        previewRun: {
          runId: 'run-old',
          sampleCount: 1,
          startedAtMs: Date.now() - 10_000_000,
        },
      })
    );
    await restoreDraft();

    // The preview mounts and its listing settles...
    await screen.findByTestId('prompt-tuning-preview');
    await screen.findByTestId('preview-prefix-empty');
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });

    // ...with no status poll for the dropped reference and no error.
    expect(apiMocks.getPreviewRun).not.toHaveBeenCalled();
    expect(screen.queryByTestId('preview-run-error')).not.toBeInTheDocument();
  });

  it('replaces the previewRun with a newly started run in the next write (Req 5.6)', async () => {
    apiMocks.getImagePreview.mockResolvedValue({
      ...emptyListing,
      total_found: 1,
      images: [
        {
          key: 'images/one.jpg',
          filename: 'one.jpg',
          size: 2048,
          last_modified: '2024-05-01T00:00:00Z',
          presigned_url: 'https://s3.example/one.jpg?sig=1',
        },
      ],
    });
    apiMocks.startPreviewRun.mockResolvedValue({
      run_id: 'run-new',
      sample_count: 1,
      status: 'Running',
    });
    apiMocks.getPreviewRun.mockResolvedValue({
      run_id: 'run-new',
      status: 'Completed',
      sample_count: 1,
      few_shot: { enabled: false, attached: 0, omitted: 0 },
      results: [],
    });

    await mountWithOffer(
      makeDraft({
        autoLabelEnabled: true,
        autoLabelModel: `llm:${NOVA.id}`,
        detectionPrompt: 'find scratches',
        tokenBudget: '9000',
        previewSelectedKeys: ['images/one.jpg'],
        previewRun: null,
      })
    );
    await restoreDraft();

    // The preview mounts with the restored selection and a startable run.
    await screen.findByTestId('preview-sample-grid');
    await waitFor(() =>
      expect(screen.getByTestId('preview-run-button')).toBeEnabled()
    );
    await act(async () => {
      fireEvent.click(screen.getByTestId('preview-run-button'));
    });
    await waitFor(() =>
      expect(apiMocks.startPreviewRun).toHaveBeenCalledTimes(1)
    );
    expect(apiMocks.startPreviewRun.mock.calls[0][0].sample_images).toEqual([
      'images/one.jpg',
    ]);

    // The next debounced write carries the new run's identity.
    await waitFor(
      () => expect(storedDraft()?.previewRun?.runId).toBe('run-new'),
      { timeout: 4000 }
    );
    const persistedRun = storedDraft()!.previewRun!;
    expect(persistedRun.sampleCount).toBe(1);
    expect(typeof persistedRun.startedAtMs).toBe('number');
  });
});

// ---------------------------------------------------------------------------
// Draft lifecycle (Req 6.1, 6.4)
// ---------------------------------------------------------------------------

describe('CreateLabelingJob recovery — draft lifecycle', () => {
  it('removes the draft before navigating after successful creation (Req 6.1)', async () => {
    let keyAtNavigate: string | null | undefined;
    navigateMock.mockImplementationOnce(() => {
      keyAtNavigate = window.localStorage.getItem(DRAFT_KEY);
    });

    await mountWithOffer(makeDraft({ activeStepIndex: 5 }));
    await restoreDraft();
    await waitFor(() => expect(screen.getByText('Team One')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Create Job' }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/labeling'));

    // The key was already gone when navigation ran...
    expect(keyAtNavigate).toBeNull();
    // ...and no pending debounced write re-creates it afterwards.
    await settlePastDebounce();
    expect(storedDraftRaw()).toBeNull();
  });

  it('keeps the draft when the wizard unmounts after edits (Req 6.4)', async () => {
    const view = render(<CreateLabelingJob />);
    await selectDdaAndTypeJobName(view.container, 'kept-after-unmount');
    await waitFor(
      () => expect(storedDraft()?.jobName).toBe('kept-after-unmount'),
      { timeout: 4000 }
    );

    view.unmount();

    // Leaving without creating keeps the setup recoverable.
    expect(storedDraft()?.jobName).toBe('kept-after-unmount');
  });
});
