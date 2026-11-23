/**
 * Smart Import for RF-DETR-Seg (rfdetr-seg-support task 12.5; Requirements
 * 8.4, 8.7, 12.5, 12.6, 15.3, 17.1; O9 a).
 *
 * Through the rendered DOM and the mocked API:
 * - the "Instance Segmentation (RF-DETR-Seg)" tile is pre-selected when the
 *   inspector suggests it for an ONNX graph, locks the runtime to ONNX
 *   (PyTorch disabled), shows class names, score and mask thresholds and the
 *   Output control, and no arch picker;
 * - it is disabled (with the other three non-detection tiles) for a
 *   convertible detection checkpoint, by the existing lock rule;
 * - the ONNX request body for the new tile: class names, both thresholds,
 *   `output_mode`, no `iou_threshold`, no `detection_arch`; the "Segmentation"
 *   tile sends no `detection_arch` either and has no arch picker;
 * - a Seg checkpoint assessment locks the new tile, disables the other four
 *   (`classification`, `object_detection`, `segmentation`,
 *   `anomaly_detection`), shows mask threshold + Output and no IoU, labels the
 *   checkpoint "RF-DETR-Seg (small)", and converts with `model_type:
 *   'instance_segmentation'`, `detection_arch: 'rf_detr_seg'`,
 *   `mask_threshold`, `output_mode` and no `iou_threshold`;
 * - the tile is hidden when `config.json` carries `rfDetrSegEnabled: false`,
 *   and a Seg graph's suggestion is then not applied.
 *
 * `SmartImport.checkpoint.test.tsx`'s harness (apiService mocked, real
 * MemoryRouter + UsecaseProvider) plus a mocked `getConfig`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import SmartImport from './SmartImport';
import { UsecaseProvider } from '../contexts/UsecaseContext';
import type { CheckpointAssessment } from '../services/api';
import type { AppConfig } from '../config';

const { apiMocks, ApiErrorClass, configState } = vi.hoisted(() => {
  class ApiErrorClass extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
  }
  return {
    ApiErrorClass,
    apiMocks: {
      listUseCases: vi.fn(),
      inspectModel: vi.fn(),
      convertModel: vi.fn(),
      getModelUploadUrl: vi.fn(),
      startPackaging: vi.fn(),
      startCompilation: vi.fn(),
    },
    configState: { current: {} as Partial<AppConfig> },
  };
});

vi.mock('../services/api', () => {
  const apiService = new Proxy(apiMocks as Record<string, unknown>, {
    get(target, prop: string) {
      if (prop in target) return target[prop];
      return (..._args: unknown[]) => Promise.resolve({});
    },
  });
  return { apiService, ApiError: ApiErrorClass };
});

vi.mock('../config', async importOriginal => {
  const actual = await importOriginal<typeof import('../config')>();
  return { ...actual, getConfig: () => configState.current as AppConfig };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const USECASE_ID = 'uc-1';
const SEG_ONNX_URI = 's3://example-bucket/models/crack-seg/model.onnx';
const CLS_ONNX_URI = 's3://example-bucket/models/classifier/model.onnx';
const SEG_PTH_URI = 's3://example-bucket/checkpoints/crack-seg/checkpoint_best_total.pth';
const PPE_URI = 's3://example-bucket/checkpoints/ppe/best.pt';

/** The inspector's answer for an RF-DETR-Seg ONNX graph (Seg_Output_Contract; backend inspect, Req 8.1). */
const SEG_ONNX_INSPECTION = {
  model_s3_uri: SEG_ONNX_URI,
  inspection_result: {
    type: 'onnx',
    architecture_hints: ['RF-DETR-Seg ONNX (dets/labels/masks, 100 queries, 2 classes)'],
    suggested_type: 'instance_segmentation',
    detection_arch: 'rf_detr_seg',
    num_classes: 2,
    num_queries: 100,
    mask_grid: 96,
    network_input: 384,
    input_width: 384,
    input_height: 384,
    num_outputs: 3,
    input_shapes: [[1, 3, 384, 384]],
    output_shapes: [[1, 100, 4], [1, 100, 3], [1, 100, 96, 96]],
  },
  supported_model_types: {},
};

/** A generic ONNX classifier: the Segmentation / Classification tiles stay as they are. */
const CLS_ONNX_INSPECTION = {
  model_s3_uri: CLS_ONNX_URI,
  inspection_result: {
    type: 'onnx',
    architecture_hints: ['ONNX graph, 1 output'],
    suggested_type: 'classification',
    num_classes: 3,
    input_width: 224,
    input_height: 224,
    num_outputs: 1,
    output_shapes: [[1, 3]],
  },
  supported_model_types: {},
};

const PPE: CheckpointAssessment = {
  kind: 'ultralytics_checkpoint',
  arch: 'yolo',
  task: 'detect',
  model_class: 'ultralytics.nn.tasks.DetectionModel',
  head_classes: ['ultralytics.nn.modules.head.Detect'],
  num_classes: 4,
  class_names: ['helmet', 'human', 'no-helmet', 'vest'],
  train_input_size: 640,
  framework: 'ultralytics',
  framework_version: '8.4.2',
  rfdetr_size: null,
  convertible: true,
  reasons: [],
};

/** An RF-DETR-Seg small checkpoint, as assess_checkpoint reports it (Req 7.2). */
const SEG_SMALL: CheckpointAssessment = {
  kind: 'rfdetr_checkpoint',
  arch: 'rf_detr_seg',
  task: 'segmentation',
  model_class: 'RFDETRSegSmall',
  head_classes: [],
  num_classes: 1,
  class_names: ['crack'],
  train_input_size: 384,
  framework: 'rfdetr',
  framework_version: null,
  rfdetr_size: null,
  model_type: 'instance_segmentation',
  rfdetr_seg_size: 'small',
  num_queries: 100,
  convertible: true,
  reasons: [],
};

/** `inspection_result` as model_converter.inspect_checkpoint_file builds it. */
function checkpointInspection(assessment: CheckpointAssessment, uri: string) {
  const size = assessment.convertible ? assessment.train_input_size : null;
  const seg = assessment.arch === 'rf_detr_seg';
  return {
    model_s3_uri: uri,
    inspection_result: {
      type: 'checkpoint',
      architecture_hints: [seg ? 'RF-DETR-Seg checkpoint' : assessment.arch === 'rf_detr' ? 'RF-DETR checkpoint' : 'Ultralytics YOLO checkpoint'],
      num_classes: assessment.num_classes,
      class_names: assessment.class_names,
      input_width: size,
      input_height: size,
      ...(assessment.convertible
        ? { suggested_type: seg ? 'instance_segmentation' : 'object_detection', detection_arch: assessment.arch }
        : {}),
      checkpoint: assessment,
      fine_tunable: true,
    },
    supported_model_types: {},
  };
}

const IMPORTED = {
  converted_model_s3_uri: 's3://example-bucket/converted-models/crack-seg/model.tar.gz',
  model_name: 'crack-seg',
  model_type: 'instance_segmentation',
  input_shape: [1, 3, 384, 384],
  model_info: { type: 'onnx', architecture_hints: [] },
  message: 'ok',
  training_id: 'imp-seg-1',
  output_mode: 'instance',
};

const CONVERSION_STARTED = {
  training_id: 'cnv-seg-1',
  model_name: 'crack-seg',
  status: 'InProgress',
  conversion: { status: 'InProgress', job_name: 'crack_seg-cnv-20261123120000' },
  fine_tunable: { arch: 'rf_detr_seg', kind: 'rfdetr_checkpoint', checkpoint_s3: 's3://example-bucket/converted-models/crack_seg-1a2b3c4d/checkpoint.pth', class_names: ['crack'], num_classes: 1 },
};

beforeEach(() => {
  vi.resetAllMocks();
  window.localStorage.clear();
  configState.current = {};
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'Cookies', account_id: '123456789012' }],
    count: 1,
  });
  apiMocks.convertModel.mockResolvedValue(IMPORTED);
  apiMocks.startPackaging.mockResolvedValue({ training_id: 'imp-seg-1', packaged_components: [], message: 'ok' });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Render + Cloudscape helpers
// ---------------------------------------------------------------------------

function TrainingDetailStub() {
  const { trainingId } = useParams();
  return <div>training-detail:{trainingId}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/models/smart-import']}>
      <UsecaseProvider>
        <Routes>
          <Route path="/models/smart-import" element={<SmartImport />} />
          <Route path="/training/:trainingId" element={<TrainingDetailStub />} />
        </Routes>
      </UsecaseProvider>
    </MemoryRouter>
  );
}

type FormFieldWrapper = ReturnType<ReturnType<typeof createWrapper>['findAllFormFields']>[number];

function findField(container: HTMLElement, label: string): FormFieldWrapper | undefined {
  return createWrapper(container)
    .findAllFormFields()
    .find(f => f.findLabel()?.getElement().textContent?.trim() === label);
}
function field(container: HTMLElement, label: string): FormFieldWrapper {
  const hit = findField(container, label);
  if (!hit) throw new Error(`No FormField labelled "${label}" is rendered`);
  return hit;
}
function tilesIn(container: HTMLElement, label: string) {
  const tiles = field(container, label).findControl()?.findTiles();
  if (!tiles) throw new Error(`FormField "${label}" has no Tiles`);
  return tiles;
}
function tileInput(container: HTMLElement, label: string, value: string): HTMLInputElement {
  const input = tilesIn(container, label).findInputByValue(value);
  if (!input) throw new Error(`No "${value}" tile under "${label}"`);
  return input.getElement();
}
function tileValues(container: HTMLElement, label: string): string[] {
  return tilesIn(container, label).findItems().map(item => (item.findNativeInput().getElement() as HTMLInputElement).value);
}
function inputIn(container: HTMLElement, label: string) {
  const input = field(container, label).findControl()?.findInput();
  if (!input) throw new Error(`FormField "${label}" has no Input`);
  return input;
}
function selectIn(container: HTMLElement, label: string) {
  const select = field(container, label).findControl()?.findSelect();
  if (!select) throw new Error(`FormField "${label}" has no Select`);
  return select;
}

async function selectUseCase(container: HTMLElement) {
  await waitFor(() => expect(apiMocks.listUseCases).toHaveBeenCalled());
  const select = field(container, 'Use Case').findControl()!.findSelect()!;
  await waitFor(() => {
    select.openDropdown();
    expect(select.findDropdown().findOptions().length).toBe(1);
  });
  select.selectOptionByValue(USECASE_ID);
}

/** Render, pick the use case, inspect `uri` with the S3 URI source. */
async function inspectFromS3(response: unknown, uri: string) {
  apiMocks.inspectModel.mockResolvedValue(response);
  const view = renderPage();
  const { container } = view;
  await selectUseCase(container);
  field(container, 'Model File (S3 URI)').findControl()!.findInput()!.setInputValue(uri);
  fireEvent.click(screen.getByRole('button', { name: 'Inspect Model' }));
  await screen.findByText('Configure Model');
  return view;
}

function setModelName(container: HTMLElement, name: string) {
  field(container, 'Model Name').findControl()!.findInput()!.setInputValue(name);
}

async function submit() {
  fireEvent.click(screen.getByRole('button', { name: 'Convert & Import Model' }));
  await waitFor(() => expect(apiMocks.convertModel).toHaveBeenCalledTimes(1));
  return apiMocks.convertModel.mock.calls[0][0] as Record<string, unknown>;
}

const ALL_TILES = ['classification', 'object_detection', 'segmentation', 'instance_segmentation', 'anomaly_detection'];

// ---------------------------------------------------------------------------
// (1) The new tile for an ONNX Seg graph (Req 8.4, 12.5)
// ---------------------------------------------------------------------------

describe('Smart Import — the Instance Segmentation (RF-DETR-Seg) tile for an ONNX graph', () => {
  it('is pre-selected from the inspector, locks ONNX, shows class names, both thresholds and Output, no arch picker', async () => {
    const { container } = await inspectFromS3(SEG_ONNX_INSPECTION, SEG_ONNX_URI);
    expect(tileValues(container, 'Model Type')).toEqual(ALL_TILES);
    expect(tileInput(container, 'Model Type', 'instance_segmentation').checked).toBe(true);
    // Nothing is locked for an ONNX graph: the other tiles stay enabled.
    for (const other of ['classification', 'object_detection', 'segmentation', 'anomaly_detection']) {
      expect(tileInput(container, 'Model Type', other).disabled).toBe(false);
    }
    // The ONNX lock of the new tile.
    expect(tileInput(container, 'Runtime / export format', 'onnx').checked).toBe(true);
    expect(tileInput(container, 'Runtime / export format', 'pytorch').disabled).toBe(true);
    // Decode settings: class names, score + mask thresholds, Output; no IoU, no arch picker.
    expect(findField(container, 'Class names')).toBeDefined();
    expect(findField(container, 'Score threshold')).toBeDefined();
    expect(findField(container, 'Mask threshold')).toBeDefined();
    expect(findField(container, 'Output')).toBeDefined();
    expect(findField(container, 'IoU threshold')).toBeUndefined();
    expect(findField(container, 'Detection architecture')).toBeUndefined();
    expect(findField(container, 'Segmentation architecture')).toBeUndefined();
    expect(findField(container, 'Input resize geometry')).toBeUndefined();
    expect(selectIn(container, 'Output').findTrigger().getElement().textContent).toContain('instance');
    // The resolution is pre-filled from the graph.
    expect(inputIn(container, 'Width').findNativeInput().getElement()).toHaveValue(384);
    expect(inputIn(container, 'Height').findNativeInput().getElement()).toHaveValue(384);
  });

  it('sends class names, both thresholds and output_mode; never iou_threshold or detection_arch (Req 8.7)', async () => {
    const { container } = await inspectFromS3(SEG_ONNX_INSPECTION, SEG_ONNX_URI);
    setModelName(container, 'crack-seg');
    inputIn(container, 'Class names').setInputValue('crack, chip');
    inputIn(container, 'Score threshold').setInputValue('0.4');
    inputIn(container, 'Mask threshold').setInputValue('0.6');
    const output = selectIn(container, 'Output');
    output.openDropdown();
    output.selectOptionByValue('semantic');
    const body = await submit();
    expect(body).toEqual({
      usecase_id: USECASE_ID,
      model_s3_uri: SEG_ONNX_URI,
      model_name: 'crack-seg',
      model_type: 'instance_segmentation',
      image_width: 384,
      image_height: 384,
      num_classes: 2,
      export_format: 'onnx',
      detection_arch: undefined,
      preserve_aspect: undefined,
      class_names: ['crack', 'chip'],
      score_threshold: 0.4,
      iou_threshold: undefined,
      mask_threshold: 0.6,
      output_mode: 'semantic',
      auto_import: true,
    });
    expect(body.iou_threshold).toBeUndefined();
    expect(body.detection_arch).toBeUndefined();
    // ONNX: straight to packaging with auto-publish, as for every ONNX import.
    await waitFor(() => expect(apiMocks.startPackaging).toHaveBeenCalledWith('imp-seg-1', undefined, true));
  });

  it('requires class names for the new tile (the backend answers 400 without them)', async () => {
    const { container } = await inspectFromS3(SEG_ONNX_INSPECTION, SEG_ONNX_URI);
    setModelName(container, 'crack-seg');
    fireEvent.click(screen.getByRole('button', { name: 'Convert & Import Model' }));
    expect(await screen.findByText(/Class names are required for Instance Segmentation/)).toBeInTheDocument();
    expect(apiMocks.convertModel).not.toHaveBeenCalled();
  });

  it('the Segmentation tile keeps the generic path: no arch picker, no detection_arch in the body', async () => {
    const { container } = await inspectFromS3(CLS_ONNX_INSPECTION, CLS_ONNX_URI);
    expect(tileInput(container, 'Model Type', 'classification').checked).toBe(true);
    tilesIn(container, 'Model Type').findItemByValue('segmentation')!.findNativeInput().click();
    await waitFor(() => expect(tileInput(container, 'Model Type', 'segmentation').checked).toBe(true));
    expect(findField(container, 'Segmentation architecture')).toBeUndefined();
    expect(findField(container, 'Detection architecture')).toBeUndefined();
    expect(findField(container, 'Mask threshold')).toBeUndefined();
    expect(findField(container, 'Output')).toBeUndefined();
    // No wording promising a semantic mask overlay anywhere on the page.
    expect(container.textContent).not.toMatch(/semantic mask overlay/i);
    // ONNX stays selectable, PyTorch stays enabled (no lock on this tile).
    expect(tileInput(container, 'Runtime / export format', 'pytorch').disabled).toBe(false);
    expect(tileInput(container, 'Runtime / export format', 'onnx').checked).toBe(true);
    setModelName(container, 'masks');
    const body = await submit();
    expect(body).toEqual(expect.objectContaining({ model_type: 'segmentation', export_format: 'onnx' }));
    expect(body.detection_arch).toBeUndefined();
    expect(body.mask_threshold).toBeUndefined();
    expect(body.output_mode).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// (2) Locks (Req 12.5; design section 8 "Seg checkpoint locks")
// ---------------------------------------------------------------------------

describe('Smart Import — checkpoint locks and the new tile', () => {
  it('a convertible detection checkpoint disables the new tile with the other non-detection tiles', async () => {
    const { container } = await inspectFromS3(checkpointInspection(PPE, PPE_URI), PPE_URI);
    await screen.findByTestId('checkpoint-panel');
    expect(tileInput(container, 'Model Type', 'object_detection').checked).toBe(true);
    for (const other of ['classification', 'segmentation', 'instance_segmentation', 'anomaly_detection']) {
      expect(tileInput(container, 'Model Type', other).disabled).toBe(true);
    }
    expect(screen.getByText('Locked to Object Detection: the checkpoint is a detector.')).toBeInTheDocument();
    expect(findField(container, 'Mask threshold')).toBeUndefined();
    expect(findField(container, 'Output')).toBeUndefined();
  });

  it('a Seg checkpoint locks the new tile, disables the other four, shows mask threshold + Output and no IoU', async () => {
    const { container } = await inspectFromS3(checkpointInspection(SEG_SMALL, SEG_PTH_URI), SEG_PTH_URI);
    await screen.findByTestId('checkpoint-panel');
    expect(screen.getByTestId('checkpoint-family').textContent).toBe('RF-DETR-Seg (small)');
    expect(screen.getByTestId('checkpoint-verdict').textContent).toBe('Can be converted to ONNX');
    expect(tileInput(container, 'Model Type', 'instance_segmentation').checked).toBe(true);
    for (const other of ['classification', 'object_detection', 'segmentation', 'anomaly_detection']) {
      expect(tileInput(container, 'Model Type', other).disabled).toBe(true);
    }
    expect(screen.getByText('Locked to Instance Segmentation: the checkpoint is an RF-DETR-Seg model.')).toBeInTheDocument();
    expect(tileInput(container, 'Runtime / export format', 'onnx').checked).toBe(true);
    expect(tileInput(container, 'Runtime / export format', 'pytorch').disabled).toBe(true);
    expect(findField(container, 'Detection architecture')).toBeUndefined();
    expect(findField(container, 'Mask threshold')).toBeDefined();
    expect(findField(container, 'Output')).toBeDefined();
    expect(findField(container, 'IoU threshold')).toBeUndefined();
    expect(inputIn(container, 'Mask threshold').findNativeInput().getElement()).toHaveValue(0.5);
    expect(screen.getByTestId('conversion-geometry').textContent).toBe('Square resize with ImageNet normalisation');
    // Its one native resolution, locked.
    const network = field(container, 'Network input');
    expect(network.findDescription()?.getElement().textContent).toContain("RF-DETR-Seg converts only at its size's native resolution (384 px)");
    expect(network.findControl()!.findSelect()!.findTrigger().getElement().textContent).toContain('384 x 384');
  });

  it('converts a Seg checkpoint with model_type instance_segmentation, detection_arch rf_detr_seg, mask_threshold and output_mode, no iou_threshold', async () => {
    apiMocks.convertModel.mockResolvedValue(CONVERSION_STARTED);
    const { container } = await inspectFromS3(checkpointInspection(SEG_SMALL, SEG_PTH_URI), SEG_PTH_URI);
    await screen.findByTestId('checkpoint-panel');
    setModelName(container, 'crack-seg');
    inputIn(container, 'Mask threshold').setInputValue('0.35');
    const output = selectIn(container, 'Output');
    output.openDropdown();
    output.selectOptionByValue('semantic');
    const body = await submit();
    expect(body).toEqual({
      usecase_id: USECASE_ID,
      model_s3_uri: SEG_PTH_URI,
      model_name: 'crack-seg',
      model_type: 'instance_segmentation',
      image_width: 384,
      image_height: 384,
      num_classes: 1,
      export_format: 'onnx',
      detection_arch: 'rf_detr_seg',
      preserve_aspect: false,
      class_names: ['crack'],
      score_threshold: 0.5,
      iou_threshold: undefined,
      mask_threshold: 0.35,
      output_mode: 'semantic',
      auto_import: true,
    });
    expect(body.iou_threshold).toBeUndefined();
    // The conversion finalizes server-side: the page navigates and never packages.
    expect(await screen.findByText('training-detail:cnv-seg-1')).toBeInTheDocument();
    expect(apiMocks.startPackaging).not.toHaveBeenCalled();
  });

  it('rejects a mask threshold outside (0, 1) before converting', async () => {
    const { container } = await inspectFromS3(checkpointInspection(SEG_SMALL, SEG_PTH_URI), SEG_PTH_URI);
    await screen.findByTestId('checkpoint-panel');
    setModelName(container, 'crack-seg');
    inputIn(container, 'Mask threshold').setInputValue('1');
    fireEvent.click(screen.getByRole('button', { name: 'Convert & Import Model' }));
    expect((await screen.findAllByText(/Mask threshold must be strictly between 0 and 1/)).length).toBeGreaterThanOrEqual(1);
    expect(apiMocks.convertModel).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// (3) The switch (Req 15.3)
// ---------------------------------------------------------------------------

describe('Smart Import — the tile is hidden when the config field is false', () => {
  it('offers the four tiles only, and does not apply a Seg graph suggestion', async () => {
    configState.current = { rfDetrSegEnabled: false };
    const { container } = await inspectFromS3(
      { ...SEG_ONNX_INSPECTION, inspection_result: { ...SEG_ONNX_INSPECTION.inspection_result, disabled_reason: 'RF-DETR-Seg is disabled on this portal' } },
      SEG_ONNX_URI
    );
    expect(tileValues(container, 'Model Type')).toEqual(['classification', 'object_detection', 'segmentation', 'anomaly_detection']);
    expect(tileInput(container, 'Model Type', 'classification').checked).toBe(true);
    expect(findField(container, 'Mask threshold')).toBeUndefined();
    expect(findField(container, 'Output')).toBeUndefined();
  });

  it('offers all five when the field is absent or true', async () => {
    configState.current = { rfDetrSegEnabled: true };
    const { container } = await inspectFromS3(CLS_ONNX_INSPECTION, CLS_ONNX_URI);
    expect(tileValues(container, 'Model Type')).toEqual(ALL_TILES);
  });
});
