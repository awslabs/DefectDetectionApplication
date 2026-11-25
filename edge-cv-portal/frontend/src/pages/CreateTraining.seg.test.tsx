/**
 * Create Training page, the RF-DETR-Seg model type (rfdetr-seg-support task
 * 12.2; Requirements 3.5, 5.6, 12.2, 12.4, 12.6, 15.2).
 *
 * Through the rendered DOM and the mocked API:
 * - the "Instance Segmentation (masks + boxes)" entry is offered under the
 *   RF-DETR source when `config.json` carries no `rfDetrSegEnabled` or
 *   `true`, and hidden for an explicit `false`;
 * - the Seg form: Size (six), Resolution (native default, block + bounds
 *   validation with the backend wording, one rejected value per block),
 *   Gradient accumulation, Learning rate, Mask threshold, Output; no IoU
 *   field and no LFV "Segmentation head only" toggle (which the LFV
 *   segmentation type still shows);
 * - switching Object Detection <-> Instance Segmentation resets the form to
 *   the arch's defaults; the size nudges the untouched instance type;
 * - the Base model control lists the six published Seg sizes, the Completed
 *   Seg job and the Seg import — never a detection record;
 * - the submitted `POST /training` body: `model_type: 'instance_segmentation'`,
 *   `detection_arch: 'rf_detr_seg'`, `output_mode`, the Seg hyperparameters
 *   (no `iou_threshold` / `imgsz` / `base_weights`);
 * - manifests: an instance manifest is ready with no transform UI; a
 *   bounding-box manifest shows the Seg error; an instance manifest under
 *   Object Detection / LFV shows the matching error;
 * - cloning a Seg job lands on the RF-DETR source with the Seg type and the
 *   Seg defaults; an InstanceSegmentation labeling job reached from Data
 *   Management preselects source and type and carries no transformation
 *   badge.
 *
 * The page is rendered inside a real `MemoryRouter` and the real
 * `UsecaseProvider`; `apiService` is mocked, and `getConfig` is mocked so
 * the switch can be flipped per test (everything else in `config` is real).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import CreateTraining from './CreateTraining';
import { UsecaseProvider } from '../contexts/UsecaseContext';
import type { DetectionArch, TrainingJobRecord } from '../services/api';
import type { AppConfig } from '../config';
import { BASE_MODEL_GROUP_LABELS, baseModelOptionValue, groupDetectionBaseModels } from '../utils/detectionBaseModels';

const { apiMocks, configState } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    listLabelingJobs: vi.fn(),
    listTrainingJobs: vi.fn(),
    listDetectionBaseModels: vi.fn(),
    validateManifest: vi.fn(),
    createTrainingJob: vi.fn(),
  },
  configState: { current: {} as Partial<AppConfig> },
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
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError };
});

vi.mock('../config', async importOriginal => {
  const actual = await importOriginal<typeof import('../config')>();
  return { ...actual, getConfig: () => configState.current as AppConfig };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const USECASE_ID = 'uc-1';
const BBOX_MANIFEST_URI = 's3://labels-bucket/blue-plate/output.manifest';
const INSTANCE_MANIFEST_URI = 's3://labels-bucket/crack-masks/output.manifest';
const DATA_PATH = 's3://images-bucket/crack-frames/';

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

const RF_JOB = record({
  training_id: 'tj-rf',
  model_name: 'blue-plate-rfdetr',
  detection: {
    detection_arch: 'rf_detr', network_input_width: 512, network_input_height: 512, resolution: 512,
    rfdetr_size: 'small', class_names: ['blue_plate'], num_classes: 1, score_threshold: 0.5, preserve_aspect: false, top_k: 300,
  },
});
const SEG_JOB = record({
  training_id: 'tj-seg',
  model_name: 'crack-masks-seg',
  model_version: '3.0.0',
  model_type: 'instance_segmentation',
  detection: {
    detection_arch: 'rf_detr_seg', network_input_width: 432, network_input_height: 432, resolution: 432,
    rfdetr_seg_size: 'medium', class_names: ['crack'], num_classes: 1, score_threshold: 0.5, mask_threshold: 0.5,
    preserve_aspect: false, top_k: 200, output_mode: 'instance',
  },
  metrics: { 'test:mAP50': 0.9 },
});
const SEG_RUNNING = record({ training_id: 'tj-seg-running', model_type: 'instance_segmentation', status: 'InProgress', detection: SEG_JOB.detection });
const IMPORTED_RF = record({
  training_id: 'imp-rf', model_name: 'imported-rfdetr', source: 'imported',
  metadata: { fine_tunable: { arch: 'rf_detr', kind: 'rfdetr', checkpoint_s3: 's3://models-bucket/imports/ckpt.pth' } },
});
const IMPORTED_SEG = record({
  training_id: 'imp-seg', model_name: 'imported-seg', model_type: 'instance_segmentation', source: 'imported',
  metadata: { fine_tunable: { arch: 'rf_detr_seg', kind: 'rfdetr', checkpoint_s3: 's3://models-bucket/imports/seg.pth', class_names: ['crack'] } },
});
const ALL_JOBS = [SEG_RUNNING, SEG_JOB, RF_JOB, IMPORTED_RF, IMPORTED_SEG];

const BBOX_SAMPLE_ENTRY = {
  'source-ref': 's3://images-bucket/blue-plate/img-0001.jpg',
  'bounding-box': { image_size: [{ width: 1920, height: 1080, depth: 3 }], annotations: [{ class_id: 0, left: 10, top: 20, width: 100, height: 50 }] },
  'bounding-box-metadata': { 'class-map': { '0': 'blue_plate' }, type: 'groundtruth/object-detection', objects: [{ confidence: 1 }], 'human-annotated': 'yes' },
};
const INSTANCE_SAMPLE_ENTRY = {
  'source-ref': 's3://images-bucket/crack-frames/img-0001.jpg',
  'instance-segmentation': {
    image_size: [{ width: 1920, height: 1080, depth: 3 }],
    annotations: [{ class_id: 0, left: 10, top: 20, width: 100, height: 50, rle: '2073600' }],
  },
  'instance-segmentation-metadata': { 'class-map': { '0': 'crack' }, type: 'groundtruth/instance-segmentation', objects: [{ confidence: 1 }], 'human-annotated': 'yes' },
};

const BBOX_LABELING_JOB = {
  job_id: 'lj-bbox', job_name: 'blue-plate-boxes', task_type: 'ObjectDetection', status: 'Completed',
  image_count: 150, created_at: 1_700_000_000, output_manifest_s3_uri: BBOX_MANIFEST_URI,
  dataset_bucket: 'images-bucket', dataset_prefix: 'blue-plate/',
};
const INSTANCE_LABELING_JOB = {
  job_id: 'lj-inst', job_name: 'crack-outlines', task_type: 'InstanceSegmentation', status: 'Completed',
  image_count: 80, created_at: 1_700_100_000, output_manifest_s3_uri: INSTANCE_MANIFEST_URI,
  dataset_bucket: 'images-bucket', dataset_prefix: 'crack-frames/',
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  configState.current = {};
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'UC1', s3_bucket: 'models-bucket' }],
    count: 1,
  });
  apiMocks.listLabelingJobs.mockResolvedValue({ jobs: [INSTANCE_LABELING_JOB, BBOX_LABELING_JOB], count: 2 });
  apiMocks.listTrainingJobs.mockResolvedValue({ jobs: ALL_JOBS, count: ALL_JOBS.length });
  apiMocks.listDetectionBaseModels.mockImplementation(async (usecaseId: string, arch: DetectionArch) => {
    const { jobs } = await apiMocks.listTrainingJobs(usecaseId);
    return groupDetectionBaseModels(jobs as TrainingJobRecord[], arch);
  });
  apiMocks.validateManifest.mockImplementation(async ({ manifest_s3_uri }: { manifest_s3_uri: string }) => ({
    valid: true,
    errors: [],
    warnings: [],
    stats: {
      total_images: 80,
      task_type: manifest_s3_uri === INSTANCE_MANIFEST_URI ? 'InstanceSegmentation' : 'ObjectDetection',
      sample_entries: [manifest_s3_uri === INSTANCE_MANIFEST_URI ? INSTANCE_SAMPLE_ENTRY : BBOX_SAMPLE_ENTRY],
    },
  }));
  apiMocks.createTrainingJob.mockResolvedValue({ training_job_id: 't1', message: 'Training job created' });
});

// ---------------------------------------------------------------------------
// Render + Cloudscape helpers (the CreateTraining.baseModel.test.tsx pattern)
// ---------------------------------------------------------------------------

type Entry = string | { pathname: string; search?: string; state?: unknown };

function renderPage(entry: Entry = '/training/create') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <UsecaseProvider>
        <Routes>
          <Route path="/training/create" element={<CreateTraining />} />
          <Route path="/training" element={<div>training-list-page</div>} />
        </Routes>
      </UsecaseProvider>
    </MemoryRouter>
  );
}

type FormFieldWrapper = ReturnType<ReturnType<typeof createWrapper>['findAllFormFields']>[number];

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
/** The option values a Select offers, in display order. */
function optionValues(container: HTMLElement, label: string): string[] {
  const select = selectIn(container, label);
  select.openDropdown();
  const values = select.findDropdown().findOptions().map(o => o.getElement().getAttribute('data-value') ?? '');
  select.closeDropdown();
  return values;
}
const triggerText = (container: HTMLElement, label: string) =>
  selectIn(container, label).findTrigger().getElement().textContent ?? '';
const resolutionValue = (container: HTMLElement) =>
  (inputIn(container, 'Resolution').findNativeInput().getElement() as HTMLInputElement).value;

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

async function renderReady(entry?: Entry) {
  const view = renderPage(entry);
  await screen.findByText('Start Training Job');
  await waitFor(() => {
    expect(triggerText(view.container, 'Use Case')).toContain('UC1');
  });
  return view;
}

/** Render, pick the RF-DETR source and its Instance Segmentation type. */
async function renderSeg() {
  const view = await renderReady();
  const { container } = view;
  chooseOption(container, 'Model Source', 'rf_detr');
  await screen.findByText('Detection Settings');
  chooseOption(container, 'Model Type', 'instance_segmentation');
  await waitFor(() => {
    expect(triggerText(container, 'Model Type')).toContain('Instance Segmentation');
  });
  return view;
}

async function waitForBaseModels(arch: DetectionArch) {
  await waitFor(() => {
    expect(apiMocks.listDetectionBaseModels).toHaveBeenCalledWith(USECASE_ID, arch);
  });
  await act(async () => {
    await Promise.all(apiMocks.listDetectionBaseModels.mock.results.map(r => r.value));
    await new Promise(resolve => setTimeout(resolve, 0));
  });
}

async function pickLabelingJob(container: HTMLElement, manifestUri: string) {
  await waitFor(() => {
    expect(triggerText(container, 'Select Labeling Job')).toContain('Select a labeling job');
  });
  chooseOption(container, 'Select Labeling Job', manifestUri);
  await waitFor(() => {
    expect(apiMocks.validateManifest).toHaveBeenCalledWith({ usecase_id: USECASE_ID, manifest_s3_uri: manifestUri });
  });
}

async function fillRequiredFields(container: HTMLElement, manifestUri = INSTANCE_MANIFEST_URI) {
  const user = userEvent.setup();
  await user.type(inputIn(container, 'Model Name').findNativeInput().getElement(), 'crack-masks-v1');
  await pickLabelingJob(container, manifestUri);
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
// (1) The picker entry and the switch (Req 12.2, 15.2)
// ---------------------------------------------------------------------------

describe('CreateTraining — the Instance Segmentation model type under RF-DETR', () => {
  it('is offered when the config field is absent, and when it is true', async () => {
    const { container, unmount } = await renderReady();
    chooseOption(container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    expect(optionValues(container, 'Model Type')).toEqual(['object_detection', 'instance_segmentation']);
    unmount();

    configState.current = { rfDetrSegEnabled: true };
    const second = await renderReady();
    chooseOption(second.container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    expect(optionValues(second.container, 'Model Type')).toEqual(['object_detection', 'instance_segmentation']);
  });

  it('is hidden when the config field is false; YOLO and marketplace lists are unchanged', async () => {
    configState.current = { rfDetrSegEnabled: false };
    const { container } = await renderReady();
    expect(optionValues(container, 'Model Type')).toEqual([
      'classification', 'classification-robust', 'segmentation', 'segmentation-robust',
    ]);
    chooseOption(container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    expect(optionValues(container, 'Model Type')).toEqual(['object_detection']);
    chooseOption(container, 'Model Source', 'yolo');
    expect(optionValues(container, 'Model Type')).toEqual(['object_detection']);
  });
});

// ---------------------------------------------------------------------------
// (2) The Seg form (Req 12.2, 12.6)
// ---------------------------------------------------------------------------

describe('CreateTraining — the RF-DETR-Seg Detection Settings', () => {
  it('shows the six sizes, the native resolution, both thresholds and Output; no IoU, no seghead toggle', async () => {
    const { container } = await renderSeg();
    expect(optionValues(container, 'Size')).toEqual(['nano', 'small', 'medium', 'large', 'xlarge', '2xlarge']);
    expect(triggerText(container, 'Size')).toContain('small (native 384px)');
    expect(resolutionValue(container)).toBe('384');
    expect(field(container, 'Resolution').findDescription()?.getElement().textContent).toContain('multiple of 24, 240–1152. Native for small: 384');
    for (const label of ['Gradient accumulation', 'Learning rate', 'Epochs', 'Batch size', 'Patience', 'Score threshold', 'Mask threshold', 'Output', 'Base model', 'Class names']) {
      expect(findField(container, label), label).toBeDefined();
    }
    expect(inputIn(container, 'Mask threshold').findNativeInput().getElement()).toHaveValue(0.5);
    expect(triggerText(container, 'Output')).toContain('instance');
    expect(optionValues(container, 'Output')).toEqual(['instance', 'semantic']);
    expect(findField(container, 'IoU threshold')).toBeUndefined();
    expect(findField(container, 'Network input size')).toBeUndefined();
    expect(screen.queryByText('Segmentation head only')).toBeNull();
    // The Seg explanation and manifest requirements are on screen.
    expect(screen.getByText(/Instance Segmentation fine-tunes an RF-DETR-Seg model/)).toBeInTheDocument();
    expect(screen.getByText('instance-segmentation-metadata')).toBeInTheDocument();
  });

  it('the LFV segmentation type still shows the "Segmentation head only" toggle', async () => {
    const { container } = await renderReady();
    chooseOption(container, 'Model Type', 'segmentation');
    expect(await screen.findByText('Segmentation head only')).toBeInTheDocument();
    chooseOption(container, 'Model Type', 'classification');
    await waitFor(() => {
      expect(screen.queryByText('Segmentation head only')).toBeNull();
    });
  });

  it('validates the resolution per size: every native passes, one rejected value per block is named with the backend wording', async () => {
    const { container } = await renderSeg();
    const natives: Record<string, string> = { nano: '312', small: '384', medium: '432', large: '504', xlarge: '624', '2xlarge': '768' };
    for (const [size, native] of Object.entries(natives)) {
      chooseOption(container, 'Size', size);
      await waitFor(() => {
        expect(resolutionValue(container)).toBe(native);
      });
      expect(screen.queryByText(/Resolution must be a multiple of/)).toBeNull();
    }
    // nano (block 12): 320 is a multiple of 32, not of 12.
    chooseOption(container, 'Size', 'nano');
    inputIn(container, 'Resolution').setInputValue('320');
    expect(await screen.findByText('Resolution must be a multiple of 12 between 240 and 1152 for RF-DETR-Seg nano')).toBeInTheDocument();
    inputIn(container, 'Resolution').setInputValue('324');
    await waitFor(() => {
      expect(screen.queryByText(/Resolution must be a multiple of/)).toBeNull();
    });
    // small (block 24): 500 is rejected; the message names the size.
    chooseOption(container, 'Size', 'small');
    inputIn(container, 'Resolution').setInputValue('500');
    expect(await screen.findByText('Resolution must be a multiple of 24 between 240 and 1152 for RF-DETR-Seg small')).toBeInTheDocument();
    // Out of bounds, although a multiple of 24.
    inputIn(container, 'Resolution').setInputValue('1176');
    expect(await screen.findByText(/between 240 and 1152 for RF-DETR-Seg small/)).toBeInTheDocument();
    // The mask threshold rule is live too.
    inputIn(container, 'Resolution').setInputValue('384');
    inputIn(container, 'Mask threshold').setInputValue('1');
    expect(await screen.findByText('Mask threshold must be strictly between 0 and 1')).toBeInTheDocument();
  });

  it('switching between the two RF-DETR types resets the form to the arch defaults and nudges the instance type by Seg size', async () => {
    const { container } = await renderReady();
    chooseOption(container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    // RF-DETR detection: small / 512, g4dn.
    expect(resolutionValue(container)).toBe('512');
    chooseOption(container, 'Size', 'medium');
    await waitFor(() => {
      expect(triggerText(container, 'Instance Type')).toContain('ml.g5.xlarge');
    });
    // -> Instance Segmentation: the Seg defaults (small / 384), not medium / 576.
    chooseOption(container, 'Model Type', 'instance_segmentation');
    await waitFor(() => {
      expect(resolutionValue(container)).toBe('384');
    });
    expect(triggerText(container, 'Size')).toContain('small');
    await waitFor(() => {
      expect(triggerText(container, 'Instance Type')).toContain('ml.g4dn.xlarge');
    });
    // Seg large and 2xlarge take ml.g5.xlarge; medium stays on the T4.
    chooseOption(container, 'Size', 'medium');
    await waitFor(() => {
      expect(resolutionValue(container)).toBe('432');
    });
    expect(triggerText(container, 'Instance Type')).toContain('ml.g4dn.xlarge');
    chooseOption(container, 'Size', '2xlarge');
    await waitFor(() => {
      expect(triggerText(container, 'Instance Type')).toContain('ml.g5.xlarge');
    });
    expect(resolutionValue(container)).toBe('768');
    // -> back to Object Detection: the rf_detr defaults again.
    chooseOption(container, 'Model Type', 'object_detection');
    await waitFor(() => {
      expect(resolutionValue(container)).toBe('512');
    });
    expect(findField(container, 'Mask threshold')).toBeUndefined();
    expect(findField(container, 'Output')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// (3) Base model groups (Req 5.4, 12.4)
// ---------------------------------------------------------------------------

describe('CreateTraining — Seg base model groups', () => {
  it('lists the six published Seg sizes, the Completed Seg job and the Seg import; no detection record', async () => {
    const { container } = await renderSeg();
    await waitForBaseModels('rf_detr_seg');
    const groups = readBaseModelGroups(container);
    expect(Object.keys(groups)).toEqual([
      BASE_MODEL_GROUP_LABELS.published,
      BASE_MODEL_GROUP_LABELS.training_job,
      BASE_MODEL_GROUP_LABELS.imported,
    ]);
    expect(groups[BASE_MODEL_GROUP_LABELS.published]).toEqual([
      'RF-DETR-Seg nano (COCO, native 312px)',
      'RF-DETR-Seg small (COCO, native 384px) — recommended',
      'RF-DETR-Seg medium (COCO, native 432px)',
      'RF-DETR-Seg large (COCO, native 504px)',
      'RF-DETR-Seg xlarge (COCO, native 624px)',
      'RF-DETR-Seg 2xlarge (COCO, native 768px)',
    ]);
    // Not the RF-DETR detection job, not the InProgress Seg job, not the RF-DETR import.
    expect(groups[BASE_MODEL_GROUP_LABELS.training_job]).toEqual(['crack-masks-seg v3.0.0']);
    expect(groups[BASE_MODEL_GROUP_LABELS.imported]).toEqual(['imported-seg v1.0.0']);
    expect(triggerText(container, 'Base model')).toContain('RF-DETR-Seg small');
    // A published size drives the Seg size and its native resolution.
    chooseOption(container, 'Base model', baseModelOptionValue({ kind: 'published', ref: 'large' }));
    await waitFor(() => {
      expect(triggerText(container, 'Size')).toContain('large');
    });
    expect(resolutionValue(container)).toBe('504');
  });

  it('the RF-DETR detection form still lists only detection records (a Seg job appears in neither group)', async () => {
    const { container } = await renderReady();
    chooseOption(container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    await waitForBaseModels('rf_detr');
    const groups = readBaseModelGroups(container);
    expect(groups[BASE_MODEL_GROUP_LABELS.training_job]).toEqual(['blue-plate-rfdetr v1.0.0']);
    expect(groups[BASE_MODEL_GROUP_LABELS.imported]).toEqual(['imported-rfdetr v1.0.0']);
  });
});

// ---------------------------------------------------------------------------
// (4) Submit payload (Req 5.6, 12.2)
// ---------------------------------------------------------------------------

describe('CreateTraining — Seg submit payload', () => {
  it('sends model_type instance_segmentation, detection_arch rf_detr_seg, output_mode and the Seg hyperparameters', async () => {
    const { container } = await renderSeg();
    await waitForBaseModels('rf_detr_seg');
    const user = await fillRequiredFields(container);
    expect(await screen.findByText(/Instance-segmentation manifest detected/)).toBeInTheDocument();
    // No transform UI for an instance manifest (Req 3.5).
    expect(screen.queryByText('Transform Manifest Now')).toBeNull();
    expect(screen.queryByText(/must be transformed/)).toBeNull();
    chooseOption(container, 'Output', 'semantic');
    const payload = await submit(user);
    expect(payload).toEqual(
      expect.objectContaining({
        usecase_id: USECASE_ID,
        model_source: 'rf_detr',
        model_type: 'instance_segmentation',
        model_name: 'crack-masks-v1',
        dataset_manifest_s3: INSTANCE_MANIFEST_URI,
        instance_type: 'ml.g4dn.xlarge',
        detection_arch: 'rf_detr_seg',
        base_model: { kind: 'published', ref: 'small' },
        output_mode: 'semantic',
        hyperparameters: {
          rfdetr_seg_size: 'small',
          resolution: 384,
          epochs: 100,
          batch: 4,
          grad_accum: 4,
          lr: 0.0001,
          patience: 10,
          score_threshold: 0.5,
          mask_threshold: 0.5,
        },
      })
    );
    const hp = payload.hyperparameters as Record<string, unknown>;
    expect(hp).not.toHaveProperty('iou_threshold');
    expect(hp).not.toHaveProperty('imgsz');
    expect(hp).not.toHaveProperty('base_weights');
    expect(hp).not.toHaveProperty('rfdetr_size');
    expect(payload).not.toHaveProperty('class_names');
    expect(await screen.findByText('training-list-page')).toBeInTheDocument();
  });

  it('a detection job sends no output_mode (the key is Seg-only)', async () => {
    const { container } = await renderReady();
    chooseOption(container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    await waitForBaseModels('rf_detr');
    const user = await fillRequiredFields(container, BBOX_MANIFEST_URI);
    await screen.findByText(/Bounding-box manifest detected/);
    const payload = await submit(user);
    expect(payload).toEqual(expect.objectContaining({ detection_arch: 'rf_detr', model_type: 'object_detection' }));
    expect(payload).not.toHaveProperty('output_mode');
  });
});

// ---------------------------------------------------------------------------
// (5) Manifest kind vs model type (Req 3.5)
// ---------------------------------------------------------------------------

describe('CreateTraining — manifest kind for the Seg form', () => {
  it('a bounding-box manifest shows the Seg error and blocks the submit', async () => {
    const { container } = await renderSeg();
    const user = userEvent.setup();
    await user.type(inputIn(container, 'Model Name').findNativeInput().getElement(), 'crack-masks-v1');
    await pickLabelingJob(container, BBOX_MANIFEST_URI);
    const message = 'RF-DETR-Seg needs an instance-segmentation manifest (one outline per object); this manifest has boxes only';
    // In the manifest alert and in the validation summary.
    expect((await screen.findAllByText(message)).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/Bounding-box manifest detected/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Start Training' })).toBeDisabled();
  });

  it('an instance manifest under Object Detection and under LFV shows the matching error', async () => {
    const { container } = await renderReady();
    chooseOption(container, 'Model Source', 'rf_detr');
    await screen.findByText('Detection Settings');
    await pickLabelingJob(container, INSTANCE_MANIFEST_URI);
    expect((await screen.findAllByText('This is an instance-segmentation manifest; choose Instance Segmentation (RF-DETR-Seg)')).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('Transform Manifest Now')).toBeNull();
    // Switching to the Seg type turns it into the ready state.
    chooseOption(container, 'Model Type', 'instance_segmentation');
    expect(await screen.findByText(/Instance-segmentation manifest detected/)).toBeInTheDocument();
    // LFV: the detection-rule message, instance wording; never "must be transformed".
    chooseOption(container, 'Model Source', 'marketplace');
    expect((await screen.findAllByText(/This is an instance-segmentation manifest — select the RF-DETR model source/)).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('Transform Manifest Now')).toBeNull();
    expect(screen.queryByText(/must be transformed/)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// (6) Clone and the Data Management entry (Req 12.2)
// ---------------------------------------------------------------------------

describe('CreateTraining — cloning a Seg job and arriving from an InstanceSegmentation labeling job', () => {
  it('cloning a Seg job lands on the RF-DETR source with the Instance Segmentation type and the Seg defaults', async () => {
    const cloneFrom = {
      training_id: SEG_JOB.training_id,
      usecase_id: USECASE_ID,
      model_name: 'crack-masks-seg',
      model_type: 'instance_segmentation',
      detection_arch: 'rf_detr_seg',
      instance_type: 'ml.g5.xlarge',
    };
    const { container } = await renderReady({ pathname: '/training/create', state: { cloneFrom } });
    expect(triggerText(container, 'Model Source')).toContain('RF-DETR');
    expect(triggerText(container, 'Model Type')).toContain('Instance Segmentation');
    await screen.findByText('Detection Settings');
    expect(triggerText(container, 'Size')).toContain('small (native 384px)');
    expect(resolutionValue(container)).toBe('384');
    expect(findField(container, 'Mask threshold')).toBeDefined();
    expect(inputIn(container, 'Model Name').findNativeInput().getElement()).toHaveValue('crack-masks-seg-clone');
    // The cloned instance type is an explicit choice: the per-size nudge leaves it alone.
    expect(triggerText(container, 'Instance Type')).toContain('ml.g5.xlarge');
  });

  it('an InstanceSegmentation labeling job preselects the RF-DETR source and the Seg type, with an "Instance masks" badge and no transformation badge', async () => {
    const { container } = await renderReady({
      pathname: '/training/create',
      search: `?usecase_id=${USECASE_ID}&data_path=${encodeURIComponent(DATA_PATH)}`,
    });
    await waitFor(() => {
      expect(triggerText(container, 'Model Type')).toContain('Instance Segmentation');
    });
    expect(triggerText(container, 'Model Source')).toContain('RF-DETR');
    expect(await screen.findByText(/Using the labeling job that labeled/)).toBeInTheDocument();
    await waitFor(() => {
      expect(triggerText(container, 'Select Labeling Job')).toContain('crack-outlines (80 images) • Instance masks • this folder');
    });
    expect(triggerText(container, 'Select Labeling Job')).not.toContain('Not Transformed');
    expect(triggerText(container, 'Select Labeling Job')).not.toContain('Transformed');
    expect(await screen.findByText(/Instance-segmentation manifest detected/)).toBeInTheDocument();
    // The dropdown describes it as instance-segmentation labels, never "Requires transformation".
    const select = selectIn(container, 'Select Labeling Job');
    select.openDropdown();
    const texts = select.findDropdown().findOptions().map(o => o.getElement().textContent ?? '');
    expect(texts.some(t => t.includes('Instance segmentation labels'))).toBe(true);
    expect(texts.some(t => t.includes('Requires transformation'))).toBe(false);
    expect(texts.some(t => t.includes('Bounding boxes') && t.includes('Object detection labels'))).toBe(true);
    select.closeDropdown();
  });
});
