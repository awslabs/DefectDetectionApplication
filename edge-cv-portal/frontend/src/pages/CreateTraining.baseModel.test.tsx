/**
 * First render test for the Create Training page
 * (rfdetr-training-and-transfer-learning task 5.4, Requirement 8.2).
 *
 * Covers the RF-DETR source and the Base model control (Req 3.1, 3.5, 3.6,
 * 6.1, 6.2, 6.3) through the rendered DOM and the mocked API:
 * - the RF-DETR source shows the RF-DETR Detection Settings (Size,
 *   Resolution, Gradient accumulation, Learning rate) and no IoU field; the
 *   YOLO source shows IoU and no Gradient accumulation;
 * - the Base model Select groups published checkpoints / the use case's
 *   Completed detectors of the same arch / fine-tunable imports, leaving out
 *   other-arch, in-progress, LFV and ONNX-only records;
 * - picking a trained detector prefills its class names and a differing
 *   class list raises the mismatch warning;
 * - the submitted `POST /training` payload carries `detection_arch`,
 *   `base_model` and the RF-DETR hyperparameters (no `iou_threshold`, no
 *   `imgsz`), for both the published default and a trained base;
 * - RF-DETR medium nudges the untouched instance type to ml.g5.xlarge.
 *
 * The page is rendered inside a real `MemoryRouter` and the real
 * `UsecaseProvider`; only `apiService` is mocked.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import CreateTraining from './CreateTraining';
import { UsecaseProvider } from '../contexts/UsecaseContext';
import type { DetectionArch, TrainingJobRecord } from '../services/api';
import {
  BASE_MODEL_GROUP_LABELS,
  baseModelOptionValue,
  classMismatchWarning,
  groupDetectionBaseModels,
} from '../utils/detectionBaseModels';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingJobs: vi.fn(),
    listTrainingJobs: vi.fn(),
    listDetectionBaseModels: vi.fn(),
    validateManifest: vi.fn(),
    createTrainingJob: vi.fn(),
  },
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
      // Anything else the page calls on mount settles to an empty object.
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const USECASE_ID = 'uc-1';
const MANIFEST_URI = 's3://labels-bucket/blue-plate/output.manifest';
const ARTIFACT_S3 = 's3://models-bucket/training/tj-yolo/output/model.tar.gz';

const record = (over: Partial<TrainingJobRecord>): TrainingJobRecord => ({
  training_id: 'tj-x',
  usecase_id: USECASE_ID,
  model_name: 'det',
  model_version: '1.0.0',
  model_type: 'object_detection',
  runtime: 'onnx',
  status: 'Completed',
  created_at: 1,
  ...over,
});

/** Completed YOLO detector (offered for YOLO only). */
const YOLO_JOB = record({
  training_id: 'tj-yolo',
  model_name: 'blue-plate-yolo',
  model_version: '2.0.0',
  artifact_s3: ARTIFACT_S3,
  detection: {
    detection_arch: 'yolo',
    network_input_width: 1280,
    network_input_height: 1280,
    class_names: ['blue_plate'],
    num_classes: 1,
    score_threshold: 0.25,
    preserve_aspect: true,
    imgsz: 1280,
  },
});

/** Completed RF-DETR detector (offered for RF-DETR only). */
const RF_JOB = record({
  training_id: 'tj-rf',
  model_name: 'blue-plate-rfdetr',
  model_version: '1.0.0',
  artifact_s3: 's3://models-bucket/training/tj-rf/output/model.tar.gz',
  detection: {
    detection_arch: 'rf_detr',
    network_input_width: 512,
    network_input_height: 512,
    resolution: 512,
    rfdetr_size: 'small',
    class_names: ['blue_plate'],
    num_classes: 1,
    score_threshold: 0.5,
    preserve_aspect: false,
    top_k: 300,
  },
});

/** Still running: never offered. */
const RF_RUNNING = record({
  training_id: 'tj-rf-running',
  model_name: 'blue-plate-rfdetr-running',
  status: 'InProgress',
  detection: RF_JOB.detection,
});

/** LFV classification job: never offered. */
const LFV_JOB = record({
  training_id: 'tj-lfv',
  model_name: 'anomaly-classifier',
  model_type: 'classification',
  runtime: undefined,
});

/** Imported record with an RF-DETR fine-tunable checkpoint (Req 7). */
const IMPORTED_RF = record({
  training_id: 'imp-rf',
  model_name: 'imported-rfdetr',
  source: 'imported',
  metadata: {
    fine_tunable: { arch: 'rf_detr', kind: 'rfdetr', checkpoint_s3: 's3://models-bucket/imports/ckpt.pth' },
  },
});

const ALL_JOBS = [RF_RUNNING, RF_JOB, YOLO_JOB, LFV_JOB, IMPORTED_RF];

/** A Ground Truth bounding-box manifest entry naming one class. */
const DETECTION_SAMPLE_ENTRY = {
  'source-ref': 's3://images-bucket/blue-plate/img-0001.jpg',
  'bounding-box': {
    image_size: [{ width: 1920, height: 1080, depth: 3 }],
    annotations: [{ class_id: 0, left: 10, top: 20, width: 100, height: 50 }],
  },
  'bounding-box-metadata': {
    'class-map': { '0': 'blue_plate' },
    type: 'groundtruth/object-detection',
    objects: [{ confidence: 1 }],
    'human-annotated': 'yes',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'UC1', s3_bucket: 'models-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingJobs.mockResolvedValue({
    jobs: [
      {
        job_id: 'lj-1',
        job_name: 'blue-plate-boxes',
        task_type: 'ObjectDetection',
        status: 'Completed',
        image_count: 150,
        created_at: 1_700_000_000,
        output_manifest_s3_uri: MANIFEST_URI,
      },
    ],
    count: 1,
  });
  apiMocks.listTrainingJobs.mockResolvedValue({ jobs: ALL_JOBS, count: ALL_JOBS.length });
  // The real client is a thin filter over GET /training; mirror it so the
  // grouping under test is the shipped one.
  apiMocks.listDetectionBaseModels.mockImplementation(async (usecaseId: string, arch: DetectionArch) => {
    const { jobs } = await apiMocks.listTrainingJobs(usecaseId);
    return groupDetectionBaseModels(jobs as TrainingJobRecord[], arch);
  });
  apiMocks.validateManifest.mockResolvedValue({
    valid: true,
    errors: [],
    warnings: [],
    stats: {
      total_images: 150,
      task_type: 'ObjectDetection',
      label_distribution: { blue_plate: 150 },
      sample_entries: [DETECTION_SAMPLE_ENTRY],
    },
  });
  apiMocks.createTrainingJob.mockResolvedValue({ training_job_id: 't1', message: 'Training job created' });
});

// ---------------------------------------------------------------------------
// Render + Cloudscape helpers
// ---------------------------------------------------------------------------

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/training/create']}>
      <UsecaseProvider>
        <Routes>
          <Route path="/training/create" element={<CreateTraining />} />
          <Route path="/training" element={<div>training-list-page</div>} />
        </Routes>
      </UsecaseProvider>
    </MemoryRouter>
  );
}

// The package's subpath wrappers are not resolvable as type imports under the
// project's tsconfig; derive the FormField wrapper type from createWrapper.
type FormFieldWrapper = ReturnType<ReturnType<typeof createWrapper>['findAllFormFields']>[number];

/** The FormField whose label text is exactly `label`, if rendered. */
function findField(container: HTMLElement, label: string): FormFieldWrapper | undefined {
  return createWrapper(container)
    .findAllFormFields()
    .find(field => field.findLabel()?.getElement().textContent?.trim() === label);
}

function field(container: HTMLElement, label: string): FormFieldWrapper {
  const hit = findField(container, label);
  if (!hit) throw new Error(`No FormField labelled "${label}" is rendered`);
  return hit;
}

function selectIn(container: HTMLElement, label: string) {
  const select = field(container, label).findControl()?.findSelect();
  if (!select) throw new Error(`FormField "${label}" has no Select`);
  return select;
}

function inputIn(container: HTMLElement, label: string) {
  const input = field(container, label).findControl()?.findInput();
  if (!input) throw new Error(`FormField "${label}" has no Input`);
  return input;
}

function chooseOption(container: HTMLElement, label: string, value: string) {
  const select = selectIn(container, label);
  select.openDropdown();
  select.selectOptionByValue(value);
}

const triggerText = (container: HTMLElement, label: string) =>
  selectIn(container, label).findTrigger().getElement().textContent ?? '';

/**
 * Open the Base model dropdown and read it back as
 * `{ [groupLabel]: [optionLabel, ...] }` in display order.
 */
function readBaseModelGroups(container: HTMLElement): Record<string, string[]> {
  const select = selectIn(container, 'Base model');
  select.openDropdown();
  const dropdown = select.findDropdown();
  const groupLabels = dropdown.findGroups().map(g => g.getElement().textContent?.trim() ?? '');
  const out: Record<string, string[]> = {};
  groupLabels.forEach(label => { out[label] = []; });
  for (const option of dropdown.findOptions()) {
    const item = option.getElement().closest('[data-group-index]');
    const groupIndex = Number(item?.getAttribute('data-group-index'));
    const groupLabel = groupLabels[groupIndex - 1];
    const optionLabel = option.findLabel().getElement().textContent?.trim() ?? '';
    (out[groupLabel] ??= []).push(optionLabel);
  }
  select.closeDropdown();
  return out;
}

/** Render, wait for the use case to auto-select, and switch Model Source. */
async function renderWithSource(source: 'yolo' | 'rf_detr') {
  const view = renderPage();
  const { container } = view;
  await screen.findByText('Start Training Job');
  await waitFor(() => {
    expect(triggerText(container, 'Use Case')).toContain('UC1');
  });
  chooseOption(container, 'Model Source', source);
  await screen.findByText('Detection Settings');
  return view;
}

/**
 * Wait for the Base model list for `arch` to have loaded: the API was asked
 * for it, and its result (plus the loading flag) has landed in state. The
 * flush is done with `act` rather than by polling the dropdown, because
 * Cloudscape's test-utils drive the dropdown through their own `act`, which
 * `waitFor` does not tolerate.
 */
async function waitForBaseModels(arch: DetectionArch) {
  await waitFor(() => {
    expect(apiMocks.listDetectionBaseModels).toHaveBeenCalledWith(USECASE_ID, arch);
  });
  await act(async () => {
    await Promise.all(apiMocks.listDetectionBaseModels.mock.results.map(r => r.value));
    await new Promise(resolve => setTimeout(resolve, 0));
  });
}

/** Fill the rest of the form: model name + the bounding-box labeling job. */
async function fillRequiredFields(container: HTMLElement) {
  const user = userEvent.setup();
  await user.type(inputIn(container, 'Model Name').findNativeInput().getElement(), 'blue-plate-v3');

  // The placeholder flips once the completed labeling jobs have loaded.
  await waitFor(() => {
    expect(triggerText(container, 'Select Labeling Job')).toContain('Select a labeling job');
  });
  chooseOption(container, 'Select Labeling Job', MANIFEST_URI);
  await waitFor(() => {
    expect(apiMocks.validateManifest).toHaveBeenCalledWith({
      usecase_id: USECASE_ID,
      manifest_s3_uri: MANIFEST_URI,
    });
  });
  await screen.findByText(/Bounding-box manifest detected/);
  return user;
}

async function submit(user: ReturnType<typeof userEvent.setup>) {
  const button = screen.getByRole('button', { name: 'Start Training' });
  await waitFor(() => {
    expect(button).not.toBeDisabled();
  });
  await user.click(button);
  await waitFor(() => {
    expect(apiMocks.createTrainingJob).toHaveBeenCalledTimes(1);
  });
  return apiMocks.createTrainingJob.mock.calls[0][0] as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// (1) Per-arch Detection Settings (Req 3.1, 3.6)
// ---------------------------------------------------------------------------

describe('CreateTraining — Detection Settings per model source', () => {
  it('shows Size / Resolution / Gradient accumulation / Learning rate and no IoU for RF-DETR', async () => {
    // Guard: without ?data_path the page must not leak a '' child into its
    // SpaceBetween (an unkeyed empty spacer div, surfaced as a key warning).
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { container } = await renderWithSource('rf_detr');
    expect(
      consoleError.mock.calls.filter(args => String(args[0]).includes('unique "key" prop'))
    ).toEqual([]);
    consoleError.mockRestore();

    expect(findField(container, 'Size')).toBeDefined();
    expect(findField(container, 'Resolution')).toBeDefined();
    expect(findField(container, 'Gradient accumulation')).toBeDefined();
    expect(findField(container, 'Learning rate')).toBeDefined();
    expect(findField(container, 'IoU threshold')).toBeUndefined();
    expect(findField(container, 'Network input size')).toBeUndefined();

    // Model Type is forced to Object Detection by the source.
    expect(triggerText(container, 'Model Type')).toContain('Object Detection');
    // RF-DETR defaults are on screen.
    expect(triggerText(container, 'Size')).toContain('small');
    expect(inputIn(container, 'Resolution').findNativeInput().getElement()).toHaveValue(512);
  });

  it('shows IoU and Network input size and no Gradient accumulation for YOLO', async () => {
    const { container } = await renderWithSource('yolo');

    expect(findField(container, 'IoU threshold')).toBeDefined();
    expect(findField(container, 'Network input size')).toBeDefined();
    expect(findField(container, 'Gradient accumulation')).toBeUndefined();
    expect(findField(container, 'Learning rate')).toBeUndefined();
    expect(findField(container, 'Size')).toBeUndefined();
    expect(findField(container, 'Resolution')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// (2) Base model grouping (Req 6.1)
// ---------------------------------------------------------------------------

describe('CreateTraining — Base model groups', () => {
  it('offers RF-DETR published checkpoints, only the Completed RF-DETR detector, and the fine-tunable import', async () => {
    const { container } = await renderWithSource('rf_detr');
    await waitForBaseModels('rf_detr');

    const groups = readBaseModelGroups(container);
    expect(Object.keys(groups)).toEqual([
      BASE_MODEL_GROUP_LABELS.published,
      BASE_MODEL_GROUP_LABELS.training_job,
      BASE_MODEL_GROUP_LABELS.imported,
    ]);
    expect(groups[BASE_MODEL_GROUP_LABELS.published]).toEqual([
      'RF-DETR nano (COCO, native 384px)',
      'RF-DETR small (COCO, native 512px) — recommended',
      'RF-DETR medium (COCO, native 576px)',
      'RF-DETR large (COCO, native 704px)',
    ]);
    // Not the YOLO job, not the InProgress RF-DETR job, not the LFV job.
    expect(groups[BASE_MODEL_GROUP_LABELS.training_job]).toEqual(['blue-plate-rfdetr v1.0.0']);
    expect(groups[BASE_MODEL_GROUP_LABELS.imported]).toEqual(['imported-rfdetr v1.0.0']);

    // The published default (small) is what the trigger shows.
    expect(triggerText(container, 'Base model')).toContain('RF-DETR small');
  });

  it('offers only the YOLO detector under the trained group for the YOLO source', async () => {
    const { container } = await renderWithSource('yolo');
    await waitForBaseModels('yolo');

    const groups = readBaseModelGroups(container);
    expect(groups[BASE_MODEL_GROUP_LABELS.published]).toEqual([
      'yolo11n.pt (nano — fastest on device)',
      'yolo11s.pt (small — recommended)',
      'yolo11m.pt (medium — more accurate, slower)',
    ]);
    expect(groups[BASE_MODEL_GROUP_LABELS.training_job]).toEqual(['blue-plate-yolo v2.0.0']);
    // The RF-DETR import is not fine-tunable for YOLO: no imported group.
    expect(groups[BASE_MODEL_GROUP_LABELS.imported]).toBeUndefined();
    expect(triggerText(container, 'Base model')).toContain('yolo11s.pt');
  });
});

// ---------------------------------------------------------------------------
// (3) Class-name prefill + mismatch warning (Req 6.2)
// ---------------------------------------------------------------------------

describe('CreateTraining — trained base model prefills class names', () => {
  it('prefills the base classes, then warns when the class list is edited to differ', async () => {
    const { container } = await renderWithSource('rf_detr');
    await waitForBaseModels('rf_detr');

    chooseOption(container, 'Base model', baseModelOptionValue({ kind: 'training_job', ref: RF_JOB.training_id }));

    const classNames = inputIn(container, 'Class names');
    await waitFor(() => {
      expect(classNames.findNativeInput().getElement()).toHaveValue('blue_plate');
    });
    const warning = classMismatchWarning(['blue_plate']);
    expect(screen.queryByText(warning)).toBeNull();
    // The base fixes the size; the Size select is locked to it.
    expect(selectIn(container, 'Size').findTrigger().getElement()).toBeDisabled();

    classNames.setInputValue('blue_plate, luggage');
    expect(await screen.findByText(warning)).toBeInTheDocument();

    // Back to the base's own list: the warning goes away.
    classNames.setInputValue('blue_plate');
    await waitFor(() => {
      expect(screen.queryByText(warning)).toBeNull();
    });
  });
});

// ---------------------------------------------------------------------------
// (4) Submit payload (Req 3.2, 3.6, 6.3)
// ---------------------------------------------------------------------------

describe('CreateTraining — RF-DETR submit payload', () => {
  it('sends detection_arch rf_detr, the published small base, and RF-DETR hyperparameters without IoU/imgsz', async () => {
    const { container } = await renderWithSource('rf_detr');
    await waitForBaseModels('rf_detr');
    const user = await fillRequiredFields(container);

    const payload = await submit(user);

    expect(payload).toEqual(
      expect.objectContaining({
        usecase_id: USECASE_ID,
        model_source: 'rf_detr',
        model_type: 'object_detection',
        model_name: 'blue-plate-v3',
        dataset_manifest_s3: MANIFEST_URI,
        instance_type: 'ml.g4dn.xlarge',
        detection_arch: 'rf_detr',
        base_model: { kind: 'published', ref: 'small' },
        hyperparameters: {
          rfdetr_size: 'small',
          resolution: 512,
          epochs: 100,
          batch: 4,
          grad_accum: 4,
          lr: 0.0001,
          patience: 10,
          score_threshold: 0.5,
        },
      })
    );
    const hp = payload.hyperparameters as Record<string, unknown>;
    expect(hp).not.toHaveProperty('iou_threshold');
    expect(hp).not.toHaveProperty('imgsz');
    expect(hp).not.toHaveProperty('base_weights');
    // No class override typed: the backend reads the manifest's class-map.
    expect(payload).not.toHaveProperty('class_names');

    expect(await screen.findByText('training-list-page')).toBeInTheDocument();
  });

  it('sends base_model {training_job, <id>} with the prefilled class names when a trained detector is chosen', async () => {
    const { container } = await renderWithSource('rf_detr');
    await waitForBaseModels('rf_detr');
    chooseOption(container, 'Base model', baseModelOptionValue({ kind: 'training_job', ref: RF_JOB.training_id }));
    const user = await fillRequiredFields(container);

    const payload = await submit(user);

    expect(payload).toEqual(
      expect.objectContaining({
        detection_arch: 'rf_detr',
        base_model: { kind: 'training_job', ref: RF_JOB.training_id },
        class_names: ['blue_plate'],
        hyperparameters: expect.objectContaining({
          rfdetr_size: 'small',
          resolution: 512,
          grad_accum: 4,
          lr: 0.0001,
        }),
      })
    );
    expect(payload.hyperparameters).not.toHaveProperty('iou_threshold');
    expect(payload.hyperparameters).not.toHaveProperty('imgsz');
  });
});

// ---------------------------------------------------------------------------
// (5) Instance nudge (Req 3.5)
// ---------------------------------------------------------------------------

describe('CreateTraining — RF-DETR size nudges the instance type', () => {
  it('moves an untouched instance type to ml.g5.xlarge for medium and back for small', async () => {
    const { container } = await renderWithSource('rf_detr');
    expect(triggerText(container, 'Instance Type')).toContain('ml.g4dn.xlarge');

    chooseOption(container, 'Size', 'medium');
    await waitFor(() => {
      expect(triggerText(container, 'Instance Type')).toContain('ml.g5.xlarge');
    });
    // Resolution followed the size's native value.
    expect(inputIn(container, 'Resolution').findNativeInput().getElement()).toHaveValue(576);

    chooseOption(container, 'Size', 'small');
    await waitFor(() => {
      expect(triggerText(container, 'Instance Type')).toContain('ml.g4dn.xlarge');
    });
  });
});
