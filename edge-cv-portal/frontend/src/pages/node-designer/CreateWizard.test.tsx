/**
 * Component tests for the custom node create wizard (custom-node-designer
 * task 12.7, Requirements 1.1, 1.5): the wizard collects name,
 * description, palette category, Port declarations, parameter
 * declarations, and Target_Architectures (1.1); the generated
 * Plugin_Scaffold is previewed with a project zip download (1.5).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CreateWizard from './CreateWizard';

const { navigateMock, listUseCases, createScaffoldPlugin, downloadZipMock } = vi.hoisted(
  () => ({
    navigateMock: vi.fn(),
    listUseCases: vi.fn(),
    createScaffoldPlugin: vi.fn(),
    downloadZipMock: vi.fn(),
  })
);

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

vi.mock('../../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  return { ApiError, apiService: { listUseCases } };
});

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    createScaffoldPlugin,
    putVersionSource: vi.fn(),
    startBuilds: vi.fn(),
  },
}));

vi.mock('./zip', () => ({
  downloadZip: downloadZipMock,
}));

const SCAFFOLD_FILES = {
  'README.md': '# Blur Regions',
  'meson.build': "project('blur-regions', 'c')",
  'python/blur_regions_hook.py': 'def process_frame(frame, params):\n    return frame\n',
};

const PLUGIN = {
  plugin_id: 'p-1',
  version: 1,
  usecase_id: 'uc-1',
  name: 'Blur Regions',
  description: '',
  kind: 'scaffold',
  deepstream: false,
  provenance: {},
  lifecycle_state: 'dev',
  review: { decision: 'pending' },
  artifacts: {},
  component: {},
  source_s3_prefix: 'plugin-sources/uc-1/p-1/1/',
  created_by: 'user',
  created_at: 1,
  updated_at: 1,
};

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
  createScaffoldPlugin.mockResolvedValue({ plugin: PLUGIN, files: SCAFFOLD_FILES });
});

/** Fill the Name field on the details step. */
function fillName(container: HTMLElement, value: string) {
  const input = container.querySelector('input[placeholder="Blur Regions"]')!;
  fireEvent.change(input, { target: { value } });
}

function clickNext() {
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
}

describe('CreateWizard', () => {
  it('collects name, description, category, ports, parameters, and architectures across the steps (1.1)', async () => {
    const { container } = render(<CreateWizard />);
    await waitFor(() => expect(listUseCases).toHaveBeenCalled());

    // Step 1: node details — use case, name, description, category.
    expect(screen.getByText('Use case')).toBeInTheDocument();
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(container.querySelector('input[placeholder="Blur Regions"]')).not.toBeNull();
    expect(
      container.querySelector('input[placeholder="Blurs configured regions in each frame"]')
    ).not.toBeNull();
    expect(screen.getByText('Palette category')).toBeInTheDocument();

    fillName(container, 'Blur Regions');
    clickNext();

    // Step 2: typed input/output Port declarations.
    expect(screen.getByText('Input ports')).toBeInTheDocument();
    expect(screen.getByText('Output ports')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add input port' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add output port' })).toBeInTheDocument();
    expect(screen.getAllByText('Port name').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Port type').length).toBeGreaterThan(0);
    clickNext();

    // Step 3: parameter declarations.
    expect(screen.getByRole('button', { name: 'Add parameter' })).toBeInTheDocument();
    clickNext();

    // Step 4: Target_Architecture multi-select.
    expect(screen.getAllByText('Target architectures').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Generate scaffold' })).toBeInTheDocument();
  });

  it('submits the assembled declaration and previews the scaffold with a zip download (1.1, 1.5)', async () => {
    const { container } = render(<CreateWizard />);
    await waitFor(() => expect(listUseCases).toHaveBeenCalled());

    fillName(container, 'Blur Regions');
    clickNext();
    clickNext();
    clickNext();
    fireEvent.click(screen.getByRole('button', { name: 'Generate scaffold' }));

    await waitFor(() => expect(createScaffoldPlugin).toHaveBeenCalledTimes(1));
    expect(createScaffoldPlugin).toHaveBeenCalledWith(
      expect.objectContaining({
        usecase_id: 'uc-1',
        name: 'Blur Regions',
        declaration: expect.objectContaining({
          typeId: 'custom.blur_regions',
          displayName: 'Blur Regions',
          category: 'preprocessing',
          inputs: [{ name: 'in', portType: 'VideoFrames' }],
          outputs: [{ name: 'out', portType: 'VideoFrames' }],
          architectures: ['x86_64'],
        }),
      })
    );

    // Scaffold review phase: every generated file is previewed as a tab.
    await screen.findByText('Scaffold for Blur Regions');
    for (const path of Object.keys(SCAFFOLD_FILES)) {
      expect(screen.getByText(path)).toBeInTheDocument();
    }

    // Zip download of the complete scaffold project (1.5).
    fireEvent.click(screen.getByRole('button', { name: 'Download zip' }));
    expect(downloadZipMock).toHaveBeenCalledWith(SCAFFOLD_FILES, 'Blur-Regions');
  });
});
