/**
 * The reference-files field of a Bedrock Inference node's
 * `reference_payload_path` (inspection-format tuning fix, design §9.13.9):
 * AC-43.
 *
 * Rendered through NodeConfigPanel, as the workflow editor renders it, with
 * the API service and `fetch` mocked. A stateful harness feeds every emitted
 * parameters record back into the panel, as the builder does, and keeps the
 * emitted records.
 *
 * On the device's commit c299a6e1 the field does not exist: the panel shows
 * the generic text control for `reference_payload_path`, so the row labels,
 * the "Upload file" button and the field's texts are not found.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import NodeConfigPanel from './NodeConfigPanel';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import type { JsonValue, NodeTypeDescriptor } from './types';
import type { UserRole } from '../../types';

const api = vi.hoisted(() => ({
  listModels: vi.fn(),
  listDevices: vi.fn(),
  getDeviceCameras: vi.fn(),
  listTuningWorkflows: vi.fn(),
  getReferenceFileUploadUrl: vi.fn(),
  completeReferenceFileUpload: vi.fn(),
  listReferenceFiles: vi.fn(),
  useUsecase: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    listModels: api.listModels,
    listDevices: api.listDevices,
    getDeviceCameras: api.getDeviceCameras,
    listTuningWorkflows: api.listTuningWorkflows,
    getReferenceFileUploadUrl: api.getReferenceFileUploadUrl,
    completeReferenceFileUpload: api.completeReferenceFileUpload,
    listReferenceFiles: api.listReferenceFiles,
  },
}));
vi.mock('../../contexts/UsecaseContext', () => ({ useUsecase: api.useUsecase }));

const WORKFLOW_ID = 'wf-ref-1';
const OWNER = '123456789012';
const MISSING_TEXT = 'This uploaded file is not stored in this workflow. Upload it again.';
const SAVE_FIRST_TEXT = 'Save the workflow once to upload reference files.';

const BEDROCK: NodeTypeDescriptor = {
  typeId: 'bedrock_inference',
  category: 'inference',
  displayName: 'Bedrock Inference',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'InferenceMeta' }],
  parameters: [
    { name: 'prompt', paramType: 'string', required: false, default: '', constraints: {} },
    {
      name: 'answer_format',
      paramType: 'enum',
      required: false,
      default: 'legacy',
      constraints: { values: ['legacy', 'inspection'] },
    },
    {
      name: 'reference_payload_path',
      paramType: 'string',
      required: false,
      default: '',
      constraints: {},
      multiline: true,
    },
  ],
  mappings: [],
  hardwareDependent: false,
};

function record(sha: string, overrides: Record<string, unknown> = {}) {
  return {
    schema: 1,
    name: `${sha}.png`,
    line: `upload://${sha}.png`,
    sha256: sha,
    ext: 'png',
    kind: 'image' as const,
    format: 'png',
    bytes: 12288,
    width: 640,
    height: 480,
    fileName: 'legend.png',
    uploadedBy: 'user-1',
    uploadedAt: 1,
    ...overrides,
  };
}

const LEGEND = record('a'.repeat(64));
const LEGEND_V2 = record('b'.repeat(64), { fileName: 'legend-v2.png', bytes: 2048 });
const LEGEND_TEXT = 'legend.png · PNG · 12 KB · 640 × 480 px';
const STAGING_KEY = `workflow-reference-staging/${WORKFLOW_ID}/${'c'.repeat(32)}`;
const UPLOAD_URL = 'https://upload.example.test/staged?X-Amz-Signature=abc';

function Harness({
  initial,
  emitted,
  role = 'DataScientist',
  workflowId = WORKFLOW_ID,
}: {
  initial: Record<string, JsonValue>;
  emitted: Record<string, JsonValue>[];
  role?: UserRole;
  workflowId?: string | null;
}) {
  const [parameters, setParameters] = useState(initial);
  const node: BuilderNode = {
    id: 'check',
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor: BEDROCK, parameters, validationMessages: [] },
  };
  return (
    <MemoryRouter>
      <NodeConfigPanel
        node={node}
        role={role}
        workflowId={workflowId}
        onParametersChange={(_nodeId, updated) => {
          emitted.push(updated);
          setParameters(updated);
        }}
      />
    </MemoryRouter>
  );
}

function renderField(
  reference: string,
  options: { answerFormat?: string; role?: UserRole; workflowId?: string | null } = {}
) {
  const emitted: Record<string, JsonValue>[] = [];
  const view = render(
    <Harness
      initial={{ answer_format: options.answerFormat ?? 'inspection', reference_payload_path: reference }}
      emitted={emitted}
      role={options.role}
      workflowId={options.workflowId === undefined ? WORKFLOW_ID : options.workflowId}
    />
  );
  const value = () => emitted[emitted.length - 1]?.reference_payload_path;
  return { ...view, emitted, value };
}

/**
 * The field's "Upload file" control, a Cloudscape FileUpload. Its visible
 * button is aria-hidden (the native file input is the focusable control),
 * so it is found through the Cloudscape test utilities.
 */
function uploadControl(container: HTMLElement) {
  const upload = createWrapper(container).findFileUpload();
  expect(upload).not.toBeNull();
  expect(upload!.findUploadButton().getElement()).toHaveTextContent('Upload file');
  return upload!;
}

function chooseFile(container: HTMLElement, file: File) {
  fireEvent.change(uploadControl(container).findNativeInput().getElement(), {
    target: { files: [file] },
  });
}

const fetchMock = vi.fn();

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  api.listModels.mockResolvedValue({ models: [], count: 0, usecase_id: 'uc-1' });
  api.listDevices.mockResolvedValue({ devices: [], count: 0 });
  api.listTuningWorkflows.mockResolvedValue({ workflows: [] });
  api.listReferenceFiles.mockResolvedValue({ files: [] });
  api.useUsecase.mockReturnValue({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() });
  api.getReferenceFileUploadUrl.mockResolvedValue({
    uploadUrl: UPLOAD_URL,
    uploadHeaders: { 'x-amz-expected-bucket-owner': OWNER },
    stagingKey: STAGING_KEY,
    expiresInSeconds: 900,
    maxBytes: 8388608,
  });
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, status: 200 });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ReferenceFilesField', () => {
  it('labels a one-image list "Reference image" and a two-row list "Reference 1" and "Reference 2"', () => {
    const first = renderField('s3://bucket-a/legend.jpg');
    expect(screen.getByText('Reference image')).toBeInTheDocument();
    uploadControl(first.container);
    first.unmount();
    renderField('s3://bucket-a/legend.jpg\nrefs.0.image');
    expect(screen.getByText('Reference 1')).toBeInTheDocument();
    expect(screen.getByText('Reference 2')).toBeInTheDocument();
    expect(screen.queryByText('Reference image')).toBeNull();
  });

  it('Move up, Move down and Remove change the value, and an empty typed row is dropped', () => {
    const field = renderField('refs.0.image\nrefs.1.image\nrefs.2.image');
    fireEvent.click(screen.getByRole('button', { name: 'Move up line 2' }));
    expect(field.value()).toBe('refs.1.image\nrefs.0.image\nrefs.2.image');
    fireEvent.click(screen.getByRole('button', { name: 'Move down line 2' }));
    expect(field.value()).toBe('refs.1.image\nrefs.2.image\nrefs.0.image');
    expect(screen.getByRole('button', { name: 'Move up line 1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move down line 3' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Add URI or payload path' }));
    expect(screen.getByText('New line')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove line 1' }));
    expect(field.value()).toBe('refs.2.image\nrefs.0.image');
    fireEvent.change(screen.getByLabelText('Reference line 3'), {
      target: { value: 's3://bucket-a/legend.jpg' },
    });
    expect(field.value()).toBe('refs.2.image\nrefs.0.image\ns3://bucket-a/legend.jpg');
  });

  it('Upload file requests a URL, PUTs the file with uploadHeaders, completes and inserts the line', async () => {
    api.completeReferenceFileUpload.mockResolvedValue({ file: LEGEND, created: true });
    const field = renderField('refs.0.image');
    const file = new File([new Uint8Array(12288)], 'legend.png', { type: 'image/png' });
    chooseFile(field.container, file);

    await waitFor(() => expect(field.value()).toBe(`refs.0.image\n${LEGEND.line}`));
    expect(api.getReferenceFileUploadUrl).toHaveBeenCalledWith(WORKFLOW_ID, 'legend.png');
    expect(fetchMock).toHaveBeenCalledWith(UPLOAD_URL, {
      method: 'PUT',
      body: file,
      headers: { 'x-amz-expected-bucket-owner': OWNER },
    });
    expect(api.completeReferenceFileUpload).toHaveBeenCalledWith(WORKFLOW_ID, {
      stagingKey: STAGING_KEY,
      fileName: 'legend.png',
    });
    expect(await screen.findByText(LEGEND_TEXT)).toBeInTheDocument();
    expect(screen.getByText('Reference 2')).toBeInTheDocument();
    expect(api.listReferenceFiles).not.toHaveBeenCalled();
  });

  it('a PUT answered 403 shows "Upload failed (HTTP 403)" and leaves the value unchanged', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403 });
    const field = renderField('refs.0.image');
    chooseFile(field.container, new File(['png'], 'legend.png', { type: 'image/png' }));

    expect(await screen.findByText('Upload failed (HTTP 403)')).toBeInTheDocument();
    expect(api.completeReferenceFileUpload).not.toHaveBeenCalled();
    expect(field.emitted).toEqual([]);
  });

  it('refuses a file of another type before any request', async () => {
    const field = renderField('refs.0.image');
    chooseFile(field.container, new File(['gif'], 'legend.gif', { type: 'image/gif' }));
    expect(await screen.findByText(/legend\.gif is not an accepted reference file/)).toBeInTheDocument();
    expect(api.getReferenceFileUploadUrl).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('Replace uploads a file into the row it replaces', async () => {
    api.listReferenceFiles.mockResolvedValue({ files: [LEGEND] });
    api.completeReferenceFileUpload.mockResolvedValue({ file: LEGEND_V2, created: true });
    const field = renderField(`refs.0.image\n${LEGEND.line}`);
    expect(await screen.findByText(LEGEND_TEXT)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Replace line 2' }));
    fireEvent.change(screen.getByTestId('reference-replace-input'), {
      target: { files: [new File(['png'], 'legend-v2.png', { type: 'image/png' })] },
    });
    await waitFor(() => expect(field.value()).toBe(`refs.0.image\n${LEGEND_V2.line}`));
    expect(await screen.findByText('legend-v2.png · PNG · 2 KB · 640 × 480 px')).toBeInTheDocument();
  });

  it('a row whose upload record is missing shows the warning', async () => {
    api.listReferenceFiles.mockResolvedValue({ files: [LEGEND] });
    renderField(`${LEGEND.line}\nupload://${'d'.repeat(64)}.pdf`);
    expect(await screen.findByText(MISSING_TEXT)).toBeInTheDocument();
    expect(screen.getAllByText(MISSING_TEXT)).toHaveLength(1);
    expect(screen.getByText(LEGEND_TEXT)).toBeInTheDocument();
    expect(api.listReferenceFiles).toHaveBeenCalledTimes(1);
    expect(api.listReferenceFiles).toHaveBeenCalledWith(WORKFLOW_ID);
  });

  it('an unsaved workflow disables the upload buttons with the save-first text', () => {
    renderField('refs.0.image', { workflowId: null });
    expect(screen.getByRole('button', { name: 'Upload file' })).toBeDisabled();
    expect(screen.getByText(SAVE_FIRST_TEXT)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add URI or payload path' })).toBeEnabled();
  });

  it('a role that cannot edit workflows sees the rows read-only', () => {
    const field = renderField('refs.0.image', { role: 'Viewer' });
    expect(screen.getByText('Reference image')).toBeInTheDocument();
    expect(screen.getByLabelText('Reference line 1')).toHaveAttribute('readonly');
    expect(createWrapper(field.container).findFileUpload()).toBeNull();
    expect(screen.queryByRole('button', { name: 'Upload file' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove line 1' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add URI or payload path' })).toBeNull();
  });

  it('shows the shared list problems for the node’s answer_format', () => {
    renderField('refs.0.image\nrefs.1.image', { answerFormat: 'legacy' });
    expect(
      screen.getByText(
        'reference_payload_path lists 2 reference(s) or a document; only answer_format ' +
          'inspection accepts several references or documents'
      )
    ).toBeInTheDocument();
  });
});
