/**
 * Smart Import for detector checkpoints (detector-checkpoint-import task 8.2;
 * Requirements 10.1-10.6).
 *
 * Covers, through the rendered DOM and the mocked API:
 * - the Checkpoint panel for the PPE `best.pt`: family, task, saving library,
 *   class count, the class names in index order (`helmet, human, no-helmet,
 *   vest`), training size and the "Can be converted to ONNX" verdict;
 * - the locks and pre-fill for a Convertible_Checkpoint: model type, output
 *   and arch locked, class names in index order with the count locked,
 *   read-only geometry, no Neo compilation targets;
 * - the exact convert payload, navigation to the record, and that neither
 *   packaging nor compilation is ever called (the server finalizes);
 * - the Upload path: upload-url, an XHR PUT with visible progress, then
 *   inspect and convert on the returned model_s3_uri;
 * - RF-DETR: native resolution, no IoU field and none in the payload;
 * - a non-convertible checkpoint: ONNX disabled with its reasons, PyTorch
 *   (base model only) still available;
 * - 400 / 503 messages shown verbatim.
 *
 * The page is rendered inside a real `MemoryRouter` and the real
 * `UsecaseProvider`; only `apiService` (and XMLHttpRequest, for the upload)
 * is mocked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import SmartImport from './SmartImport';
import { UsecaseProvider } from '../contexts/UsecaseContext';
import type { CheckpointAssessment } from '../services/api';
import { COMPILATION_TARGETS } from '../utils/compilationTargets';

const { apiMocks, ApiErrorClass } = vi.hoisted(() => {
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

// ---------------------------------------------------------------------------
// Fixtures: the backend's inspect output for the spike's checkpoints
// ---------------------------------------------------------------------------

const USECASE_ID = 'uc-1';
const PPE_URI = 's3://ryvan-cookies/checkpoints/ppe/best.pt';
const PPE_NAMES = ['helmet', 'human', 'no-helmet', 'vest'];

const PPE: CheckpointAssessment = {
  kind: 'ultralytics_checkpoint',
  arch: 'yolo',
  task: 'detect',
  model_class: 'ultralytics.nn.tasks.DetectionModel',
  head_classes: ['ultralytics.nn.modules.head.Detect'],
  num_classes: 4,
  class_names: PPE_NAMES,
  train_input_size: 640,
  framework: 'ultralytics',
  framework_version: '8.4.2',
  rfdetr_size: null,
  convertible: true,
  reasons: [],
};

const RF_SMALL: CheckpointAssessment = {
  kind: 'rfdetr_checkpoint',
  arch: 'rf_detr',
  task: 'detect',
  model_class: 'RFDETRSmall',
  head_classes: [],
  num_classes: 1,
  class_names: ['blue_plate'],
  train_input_size: 512,
  framework: 'rfdetr',
  framework_version: null,
  rfdetr_size: 'small',
  convertible: true,
  reasons: [],
};

const SEG_REASONS = [
  'this ultralytics checkpoint is a segmentation model (SegmentationModel); only object detection converts',
  "the checkpoint was trained for the 'segment' task; only detect converts",
];

const YOLO_SEG: CheckpointAssessment = {
  ...PPE,
  model_class: 'ultralytics.nn.tasks.SegmentationModel',
  head_classes: ['ultralytics.nn.modules.head.Segment'],
  task: 'segment',
  num_classes: 80,
  class_names: null,
  framework_version: '8.3.0',
  convertible: false,
  reasons: SEG_REASONS,
};

/** `inspection_result` as model_converter.inspect_checkpoint_file builds it. */
function inspection(assessment: CheckpointAssessment, uri: string) {
  const size = assessment.convertible ? assessment.train_input_size : null;
  return {
    model_s3_uri: uri,
    inspection_result: {
      type: 'checkpoint',
      architecture_hints: [assessment.arch === 'rf_detr' ? 'RF-DETR checkpoint' : 'Ultralytics YOLO checkpoint'],
      num_classes: assessment.num_classes,
      class_names: assessment.class_names,
      input_width: size,
      input_height: size,
      ...(assessment.convertible ? { suggested_type: 'object_detection', detection_arch: assessment.arch } : {}),
      checkpoint: assessment,
      fine_tunable: true,
    },
    supported_model_types: {},
  };
}

const CONVERSION_STARTED = {
  training_id: 'cnv-1',
  model_name: 'ppe-detection',
  status: 'InProgress',
  conversion: { status: 'InProgress', job_name: 'ppe_detection-cnv-20261001120000' },
  fine_tunable: {
    arch: 'yolo',
    kind: 'ultralytics_checkpoint',
    checkpoint_s3: 's3://ryvan-cookies/converted-models/ppe_detection-1a2b3c4d/checkpoint.pt',
    class_names: PPE_NAMES,
    num_classes: 4,
  },
};

beforeEach(() => {
  // Reset (not just clear), so no test inherits another's implementations.
  vi.resetAllMocks();
  window.localStorage.clear();
  apiMocks.listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'Cookies', account_id: '164152369890' }],
    count: 1,
  });
  apiMocks.convertModel.mockResolvedValue(CONVERSION_STARTED);
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

function field(container: HTMLElement, label: string): FormFieldWrapper {
  const hit = createWrapper(container)
    .findAllFormFields()
    .find(f => f.findLabel()?.getElement().textContent?.trim() === label);
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
async function inspectFromS3(assessment: CheckpointAssessment, uri = PPE_URI) {
  apiMocks.inspectModel.mockResolvedValue(inspection(assessment, uri));
  const view = renderPage();
  const { container } = view;
  await selectUseCase(container);
  field(container, 'Model File (S3 URI)').findControl()!.findInput()!.setInputValue(uri);
  fireEvent.click(screen.getByRole('button', { name: 'Inspect Model' }));
  await screen.findByTestId('checkpoint-panel');
  await screen.findByText('Configure Model');
  return view;
}

function setModelName(container: HTMLElement, name: string) {
  field(container, 'Model Name').findControl()!.findInput()!.setInputValue(name);
}

function classNameInputs(): HTMLInputElement[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>('input[aria-label^="Class "]'))
    .filter(input => / name$/.test(input.getAttribute('aria-label') || ''));
}

async function submit() {
  fireEvent.click(screen.getByRole('button', { name: 'Convert & Import Model' }));
}

// ---------------------------------------------------------------------------
// Checkpoint panel (Req 10.2)
// ---------------------------------------------------------------------------

describe('Checkpoint panel', () => {
  it('shows the PPE checkpoint: family, task, library, classes in index order, size, verdict', async () => {
    await inspectFromS3(PPE);

    expect(apiMocks.inspectModel).toHaveBeenCalledWith({ usecase_id: USECASE_ID, model_s3_uri: PPE_URI });
    expect(screen.getByTestId('checkpoint-family').textContent).toBe('Ultralytics YOLO');
    expect(screen.getByText('detect')).toBeInTheDocument();
    expect(screen.getByTestId('checkpoint-library').textContent).toBe('ultralytics 8.4.2');
    expect(screen.getByTestId('checkpoint-class-count').textContent).toBe('4');
    expect(screen.getByTestId('checkpoint-class-names').textContent).toBe('helmet, human, no-helmet, vest');
    expect(screen.getByTestId('checkpoint-input-size').textContent).toBe('640 px');
    expect(screen.getByTestId('checkpoint-verdict').textContent).toBe('Can be converted to ONNX');
    // The generic PyTorch analysis grid (layers / channels) is not shown for checkpoints.
    expect(screen.queryByText('Model Analysis')).not.toBeInTheDocument();
  });

  it('lists every reason a checkpoint cannot be converted', async () => {
    await inspectFromS3(YOLO_SEG, 's3://ryvan-cookies/checkpoints/seg/yolo11n-seg.pt');

    expect(screen.getByTestId('checkpoint-verdict').textContent).toContain('Cannot be converted to ONNX');
    const reasons = Array.from(screen.getByTestId('checkpoint-reasons').querySelectorAll('li')).map(li => li.textContent);
    expect(reasons).toEqual(SEG_REASONS);
    expect(screen.getByTestId('checkpoint-class-names').textContent).toBe('Not stored in the checkpoint');
  });
});

// ---------------------------------------------------------------------------
// Convertible_Checkpoint: locks, pre-fill, payload, no packaging (Req 10.3, 10.5)
// ---------------------------------------------------------------------------

describe('Convertible_Checkpoint', () => {
  it('locks type, output and arch, pre-fills in index order, and offers no compilation targets', async () => {
    const { container } = await inspectFromS3(PPE);

    // Model type locked to Object Detection.
    expect(tileInput(container, 'Model Type', 'object_detection').checked).toBe(true);
    for (const other of ['classification', 'segmentation', 'anomaly_detection']) {
      expect(tileInput(container, 'Model Type', other).disabled).toBe(true);
    }
    // Output locked to ONNX; arch locked to YOLO.
    expect(tileInput(container, 'Runtime / export format', 'onnx').checked).toBe(true);
    expect(tileInput(container, 'Runtime / export format', 'pytorch').disabled).toBe(true);
    expect(tileInput(container, 'Detection architecture', 'yolo').checked).toBe(true);
    expect(tileInput(container, 'Detection architecture', 'rf_detr').disabled).toBe(true);

    // Class names in index order, one input per class; the count is locked.
    expect(classNameInputs().map(i => i.value)).toEqual(PPE_NAMES);
    expect(field(container, 'Number of Classes').findControl()!.findInput()!.isDisabled()).toBe(true);
    expect(
      field(container, 'Number of Classes').findControl()!.findInput()!.findNativeInput().getElement().value
    ).toBe('4');

    // Network input from the training size; geometry read-only; YOLO thresholds.
    expect(field(container, 'Network input').findControl()!.findInput()!.findNativeInput().getElement().value).toBe('640');
    expect(screen.getByTestId('conversion-geometry').textContent).toBe('Letterbox (aspect ratio preserved)');
    expect(screen.queryByText(/Preserve aspect ratio \(letterbox\)/)).not.toBeInTheDocument();
    expect(field(container, 'Score threshold').findControl()!.findInput()!.findNativeInput().getElement().value).toBe('0.25');
    expect(field(container, 'IoU threshold').findControl()!.findInput()!.findNativeInput().getElement().value).toBe('0.45');

    // No Neo compilation targets.
    expect(screen.queryByText('Compilation Options')).not.toBeInTheDocument();
    expect(screen.queryByText('Compilation Targets')).not.toBeInTheDocument();
  });

  it('submits the conversion payload and opens the record without calling packaging', async () => {
    const { container } = await inspectFromS3(PPE);
    setModelName(container, 'ppe-detection');
    await submit();

    await screen.findByText('training-detail:cnv-1');
    expect(apiMocks.convertModel).toHaveBeenCalledTimes(1);
    expect(apiMocks.convertModel).toHaveBeenCalledWith({
      usecase_id: USECASE_ID,
      model_s3_uri: PPE_URI,
      model_name: 'ppe-detection',
      model_type: 'object_detection',
      image_width: 640,
      image_height: 640,
      num_classes: 4,
      export_format: 'onnx',
      detection_arch: 'yolo',
      preserve_aspect: true,
      class_names: ['helmet', 'human', 'no-helmet', 'vest'],
      score_threshold: 0.25,
      iou_threshold: 0.45,
      auto_import: true,
    });
    expect(apiMocks.startPackaging).not.toHaveBeenCalled();
    expect(apiMocks.startCompilation).not.toHaveBeenCalled();
  });

  it('sends renamed classes in the same order and count', async () => {
    const { container } = await inspectFromS3(PPE);
    setModelName(container, 'ppe-detection');
    const inputs = createWrapper(container)
      .findAllInputs()
      .filter(i => / name$/.test(i.findNativeInput().getElement().getAttribute('aria-label') || ''));
    expect(inputs).toHaveLength(4);
    inputs[1].setInputValue('person');
    inputs[3].setInputValue('hi-vis');
    await submit();

    await screen.findByText('training-detail:cnv-1');
    expect(apiMocks.convertModel.mock.calls[0][0].class_names).toEqual(['helmet', 'person', 'no-helmet', 'hi-vis']);
    expect(apiMocks.startPackaging).not.toHaveBeenCalled();
  });

  it('blocks a blank class name or an out-of-bounds input before calling the API', async () => {
    const { container } = await inspectFromS3(PPE);
    setModelName(container, 'ppe-detection');
    const nameInputs = createWrapper(container)
      .findAllInputs()
      .filter(i => / name$/.test(i.findNativeInput().getElement().getAttribute('aria-label') || ''));
    nameInputs[2].setInputValue('  ');
    field(container, 'Network input').findControl()!.findInput()!.setInputValue('650');
    await submit();

    expect(
      await screen.findByText(
        'Class 2 needs a name. The network input must be a multiple of 32 between 320 and 2048 for YOLO; got 650'
      )
    ).toBeInTheDocument();
    expect(apiMocks.convertModel).not.toHaveBeenCalled();
  });

  it('RF-DETR: native resolution, square-resize geometry, and no IoU field or payload key', async () => {
    const uri = 's3://ryvan-cookies/checkpoints/rfdetr/checkpoint_best_total.pth';
    const { container } = await inspectFromS3(RF_SMALL, uri);

    expect(screen.getByTestId('checkpoint-family').textContent).toBe('RF-DETR (small)');
    expect(screen.getByTestId('checkpoint-library').textContent).toBe('rfdetr (version not recorded)');
    expect(tileInput(container, 'Detection architecture', 'rf_detr').checked).toBe(true);
    expect(tileInput(container, 'Detection architecture', 'yolo').disabled).toBe(true);
    const select = field(container, 'Network input').findControl()!.findSelect()!;
    expect(select.findTrigger().getElement().textContent).toContain('512 x 512');
    expect(select.isDisabled()).toBe(true);
    expect(screen.getByTestId('conversion-geometry').textContent).toBe('Square resize with ImageNet normalisation');
    expect(field(container, 'Score threshold').findControl()!.findInput()!.findNativeInput().getElement().value).toBe('0.5');
    expect(screen.queryByText('IoU threshold')).not.toBeInTheDocument();

    setModelName(container, 'blue-plate-rfdetr');
    await submit();
    await screen.findByText('training-detail:cnv-1');
    const payload = apiMocks.convertModel.mock.calls[0][0];
    expect(payload).toMatchObject({
      model_s3_uri: uri,
      image_width: 512,
      image_height: 512,
      num_classes: 1,
      export_format: 'onnx',
      detection_arch: 'rf_detr',
      preserve_aspect: false,
      class_names: ['blue_plate'],
      score_threshold: 0.5,
    });
    // The request body is JSON: an undefined IoU is simply absent.
    expect(JSON.parse(JSON.stringify(payload))).not.toHaveProperty('iou_threshold');
    expect(apiMocks.startPackaging).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Non-convertible checkpoint (Req 10.4) and verbatim errors (Req 10.6)
// ---------------------------------------------------------------------------

describe('non-convertible checkpoint', () => {
  it('disables ONNX with its reasons and keeps the PyTorch base-model import', async () => {
    const { container } = await inspectFromS3(YOLO_SEG, 's3://ryvan-cookies/checkpoints/seg/yolo11n-seg.pt');

    const onnx = tileInput(container, 'Runtime / export format', 'onnx');
    const pytorch = tileInput(container, 'Runtime / export format', 'pytorch');
    expect(onnx.disabled).toBe(true);
    expect(pytorch.disabled).toBe(false);
    expect(pytorch.checked).toBe(true);
    const onnxTile = tilesIn(container, 'Runtime / export format').findItemByValue('onnx')!;
    expect(onnxTile.getElement().textContent).toContain(`Unavailable: ${SEG_REASONS.join('; ')}`);
    // Nothing is locked: the user still chooses the model type.
    expect(tileInput(container, 'Model Type', 'classification').disabled).toBe(false);
    // Choosing Segmentation does not force ONNX back on for this checkpoint.
    tilesIn(container, 'Model Type').findItemByValue('segmentation')!.findNativeInput().click();
    await waitFor(() => expect(tileInput(container, 'Model Type', 'segmentation').checked).toBe(true));
    expect(tileInput(container, 'Runtime / export format', 'pytorch').checked).toBe(true);
  });

  it('offers the same auto-compile targets as the Compilation tab, ONNX export included', async () => {
    const { container } = await inspectFromS3(YOLO_SEG, 's3://ryvan-cookies/checkpoints/seg/yolo11n-seg.pt');

    const picker = field(container, 'Compilation Targets').findControl()!.findMultiselect()!;
    picker.openDropdown();
    const offered = picker
      .findDropdown()
      .findOptions()
      .map(o => o.findLabel().getElement().textContent?.trim());
    picker.closeDropdown();
    expect(offered).toEqual(COMPILATION_TARGETS.map(t => t.name));
    expect(offered).toContain('ONNX Runtime (portable)');
  });
});

describe('verbatim errors', () => {
  it('shows a 503 from convert exactly as the server wrote it', async () => {
    const message = 'Checkpoint conversion is not configured on this portal (no detector export image)';
    apiMocks.convertModel.mockRejectedValue(new ApiErrorClass(message, 503));
    const { container } = await inspectFromS3(PPE);
    setModelName(container, 'ppe-detection');
    await submit();

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByText(/training-detail:/)).not.toBeInTheDocument();
    expect(apiMocks.startPackaging).not.toHaveBeenCalled();
  });

  it('shows a 400 from inspect exactly as the server wrote it', async () => {
    const message = 'Checkpoint is 600000000 bytes; the checkpoint size cap is 536870912 bytes (512 MiB)';
    apiMocks.inspectModel.mockRejectedValue(new ApiErrorClass(message, 400));
    const { container } = renderPage();
    await selectUseCase(container);
    field(container, 'Model File (S3 URI)').findControl()!.findInput()!.setInputValue(PPE_URI);
    fireEvent.click(screen.getByRole('button', { name: 'Inspect Model' }));

    expect(await screen.findByText(message)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Upload a file (Req 10.1)
// ---------------------------------------------------------------------------

class FakeXhr {
  static instances: FakeXhr[] = [];
  method = '';
  url = '';
  body: unknown = null;
  status = 0;
  responseText = '';
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  constructor() {
    FakeXhr.instances.push(this);
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader() {
    throw new Error('the upload must not set request headers');
  }
  send(body: unknown) {
    this.body = body;
  }
}

describe('Upload a file', () => {
  it('gets an upload URL, PUTs with progress, then inspects and converts the returned URI', async () => {
    FakeXhr.instances = [];
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const uploadedUri = 's3://ryvan-cookies/model-uploads/5b0c7a51-0000-4000-8000-000000000001/best.pt';
    apiMocks.getModelUploadUrl.mockResolvedValue({
      upload_url: 'https://ryvan-cookies.s3.amazonaws.com/model-uploads/5b0c/best.pt?X-Amz-Signature=abc',
      model_s3_uri: uploadedUri,
      expires_in: 900,
    });
    apiMocks.inspectModel.mockResolvedValue(inspection(PPE, uploadedUri));

    const { container } = renderPage();
    await selectUseCase(container);
    createWrapper(container).findSegmentedControl()!.findSegmentById('upload')!.click();

    const file = new File([new Uint8Array(2048)], 'best.pt');
    fireEvent.change(createWrapper(container).findFileUpload()!.findNativeInput().getElement(), {
      target: { files: [file] },
    });
    const uploadButton = screen.getByRole('button', { name: 'Upload and inspect' });
    await waitFor(() => expect(uploadButton).not.toBeDisabled());
    fireEvent.click(uploadButton);

    await waitFor(() => expect(FakeXhr.instances).toHaveLength(1));
    expect(apiMocks.getModelUploadUrl).toHaveBeenCalledWith({
      usecase_id: USECASE_ID,
      file_name: 'best.pt',
      size_bytes: 2048,
    });
    const xhr = FakeXhr.instances[0];
    expect(xhr.method).toBe('PUT');
    expect(xhr.url).toBe('https://ryvan-cookies.s3.amazonaws.com/model-uploads/5b0c/best.pt?X-Amz-Signature=abc');
    expect(xhr.body).toBe(file);

    // Progress is visible while the PUT runs.
    act(() => xhr.upload.onprogress!({ lengthComputable: true, loaded: 1024, total: 2048 }));
    expect(await screen.findByText('50%')).toBeInTheDocument();
    expect(apiMocks.inspectModel).not.toHaveBeenCalled();

    act(() => {
      xhr.status = 200;
      xhr.onload!();
    });

    // The returned model_s3_uri is what inspect and convert use.
    await screen.findByTestId('checkpoint-panel');
    expect(apiMocks.inspectModel).toHaveBeenCalledWith({ usecase_id: USECASE_ID, model_s3_uri: uploadedUri });
    expect(screen.getByTestId('checkpoint-class-names').textContent).toBe('helmet, human, no-helmet, vest');

    await screen.findByText('Configure Model');
    setModelName(container, 'ppe-detection');
    await submit();
    await screen.findByText('training-detail:cnv-1');
    expect(apiMocks.convertModel.mock.calls[0][0].model_s3_uri).toBe(uploadedUri);
    expect(apiMocks.startPackaging).not.toHaveBeenCalled();
  });

  it('refuses a file type the upload route would reject, without asking for a URL', async () => {
    const { container } = renderPage();
    await selectUseCase(container);
    createWrapper(container).findSegmentedControl()!.findSegmentById('upload')!.click();
    fireEvent.change(createWrapper(container).findFileUpload()!.findNativeInput().getElement(), {
      target: { files: [new File(['x'], 'weights.bin')] },
    });

    expect(await screen.findByText('The file must end in .pt, .pth, .onnx; got weights.bin')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload and inspect' })).toBeDisabled();
    expect(apiMocks.getModelUploadUrl).not.toHaveBeenCalled();
  });

  it('shows an upload-url 400 verbatim and does not upload', async () => {
    FakeXhr.instances = [];
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const message = 'Insufficient permissions';
    apiMocks.getModelUploadUrl.mockRejectedValue(new ApiErrorClass(message, 403));
    const { container } = renderPage();
    await selectUseCase(container);
    createWrapper(container).findSegmentedControl()!.findSegmentById('upload')!.click();
    fireEvent.change(createWrapper(container).findFileUpload()!.findNativeInput().getElement(), {
      target: { files: [new File(['x'], 'best.pt')] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Upload and inspect' }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(FakeXhr.instances).toHaveLength(0);
    expect(apiMocks.inspectModel).not.toHaveBeenCalled();
  });
});
