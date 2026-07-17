/**
 * Component tests for the Node_Designer Code_Assistant integration
 * (custom-node-code-assist task 8.2, Requirements 1.3, 2.5, 6.2, 6.5).
 *
 * In both the CreateWizard scaffold review and the GeneratePanel
 * generated-source review: the assistant renders on the `.py` scaffold
 * tab only — C sources, meson files, and READMEs get no assistant (1.3);
 * the panel is omitted entirely for users without the UseCaseAdmin or
 * PortalAdmin role (6.2, 6.5); accepting reviewed code replaces exactly
 * that file's content in the files map, leaving every sibling file
 * untouched (2.5). The panel's own flows are covered by
 * CodeAssistPanel.test.tsx.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import CreateWizard from './CreateWizard';
import GeneratePanel from './GeneratePanel';

const {
  navigateMock,
  listUseCases,
  codeAssist,
  authState,
  createScaffoldPlugin,
  startGeneration,
  getGenerationTurn,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  codeAssist: vi.fn(),
  /** Mutable per-test auth role consumed by the useAuth mock. */
  authState: { role: 'UseCaseAdmin' },
  createScaffoldPlugin: vi.fn(),
  startGeneration: vi.fn(),
  getGenerationTurn: vi.fn(),
}));

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
  return { ApiError, apiService: { listUseCases, codeAssist } };
});

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'u-1',
      email: 'user@example.com',
      username: 'user',
      role: authState.role,
      is_super_user: false,
    },
  }),
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    createScaffoldPlugin,
    startGeneration,
    getGenerationTurn,
    continueGeneration: vi.fn(),
    acceptGeneration: vi.fn(),
    putVersionSource: vi.fn(),
    startBuilds: vi.fn(),
  },
}));

vi.mock('./zip', () => ({ downloadZip: vi.fn() }));

// -------------------------------------------------------------- fixtures

const HOOK_PATH = 'plugin/frame_processing_hook.py';
const HOOK_CODE = 'def process_frame(frame, params):\n    return frame\n';

/** Scaffold with one Python hook among C/meson/README siblings. */
const SCAFFOLD_FILES: Record<string, string> = {
  'README.md': '# Blur Regions',
  'meson.build': "project('blur-regions', 'c')",
  [HOOK_PATH]: HOOK_CODE,
  'src/gstblurregions.c': '/* GStreamer element */',
};

const NON_PY_PATHS = ['README.md', 'meson.build', 'src/gstblurregions.c'];

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

const GENERATED_CODE =
  'def process_frame(frame, params):\n    return frame[::-1]\n';

const ASSIST_SUCCESS = {
  code: GENERATED_CODE,
  notes: 'Flips the frame vertically.',
  model_id: 'us.anthropic.test-model',
  contract: 'frame_hook',
};

// --------------------------------------------------------------- helpers

/** The assistant's prompt textarea (accessible name = FormField label). */
const assistantPrompt = () =>
  screen.queryByRole('textbox', { name: 'Code assistant' });

/** Source textarea of a scaffold file tab. */
const sourceEditor = (path: string) =>
  screen.getByLabelText(`Source of ${path}`) as HTMLTextAreaElement;

function openTab(container: HTMLElement, path: string) {
  createWrapper(container).findTabs()!.findTabLinkById(path)!.click();
}

/** Drive the CreateWizard through the steps into the scaffold review. */
async function reachCreateWizardScaffold() {
  const { container } = render(<CreateWizard />);
  await waitFor(() => expect(listUseCases).toHaveBeenCalled());
  const nameInput = container.querySelector('input[placeholder="Blur Regions"]')!;
  fireEvent.change(nameInput, { target: { value: 'Blur Regions' } });
  for (let i = 0; i < 3; i += 1) {
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  }
  fireEvent.click(screen.getByRole('button', { name: 'Generate scaffold' }));
  await screen.findByText('Scaffold for Blur Regions');
  return container;
}

/** Drive the GeneratePanel through a completed turn into the chat phase. */
async function reachGeneratePanelSource() {
  startGeneration.mockResolvedValue({
    session_id: 's-1',
    usecase_id: 'uc-1',
    turn_status: 'pending',
  });
  getGenerationTurn.mockResolvedValue({
    session_id: 's-1',
    usecase_id: 'uc-1',
    turn_status: 'completed',
    files: SCAFFOLD_FILES,
    assistant_text: 'Generated the scaffold.',
    model_id: 'model-1',
  });
  const { container } = render(<GeneratePanel />);
  await waitFor(() => expect(listUseCases).toHaveBeenCalled());
  const nameInput = container.querySelector('input[placeholder="Blur Regions"]')!;
  fireEvent.change(nameInput, { target: { value: 'Blur Regions' } });
  fireEvent.change(screen.getByLabelText('Node behavior prompt'), {
    target: { value: 'Blur every region in each frame' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Generate scaffold' }));
  await screen.findByText('Generated source');
  return container;
}

/** Submit an assist prompt on the open `.py` tab and accept the review. */
async function assistAndAccept() {
  codeAssist.mockResolvedValue(ASSIST_SUCCESS);
  fireEvent.change(assistantPrompt()!, {
    target: { value: 'Flip the frame vertically' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
  await screen.findByLabelText('Generated code for review');
  fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.role = 'UseCaseAdmin';
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
  createScaffoldPlugin.mockResolvedValue({ plugin: PLUGIN, files: SCAFFOLD_FILES });
});

// ----------------------------------------------------------------- tests

describe('CreateWizard Code_Assistant integration', () => {
  it('renders the assistant on the .py tab and on no other scaffold tab (1.3)', async () => {
    const container = await reachCreateWizardScaffold();

    // Default active tab is the first sorted path (README.md): no panel.
    expect(sourceEditor('README.md')).toBeInTheDocument();
    expect(assistantPrompt()).toBeNull();

    // The Python hook tab carries the assistant beside its editor.
    openTab(container, HOOK_PATH);
    expect(sourceEditor(HOOK_PATH)).toHaveValue(HOOK_CODE);
    expect(assistantPrompt()).toBeInTheDocument();

    // C source and meson tabs get no assistant.
    for (const path of ['meson.build', 'src/gstblurregions.c']) {
      openTab(container, path);
      expect(sourceEditor(path)).toBeInTheDocument();
      expect(assistantPrompt()).toBeNull();
    }
  });

  it('omits the assistant entry point for non-admin users (6.2, 6.5)', async () => {
    authState.role = 'DataScientist';
    const container = await reachCreateWizardScaffold();

    openTab(container, HOOK_PATH);
    // The editor itself is still there; only the assistant is gone.
    expect(sourceEditor(HOOK_PATH)).toHaveValue(HOOK_CODE);
    expect(assistantPrompt()).toBeNull();
    expect(screen.queryByText('Code assistant')).toBeNull();
  });

  it('Accept replaces exactly the .py file in the files map (2.5)', async () => {
    const container = await reachCreateWizardScaffold();
    openTab(container, HOOK_PATH);

    await assistAndAccept();

    // The request targeted this surface, contract, and file content.
    expect(codeAssist).toHaveBeenCalledTimes(1);
    expect(codeAssist).toHaveBeenCalledWith(
      expect.objectContaining({
        usecase_id: 'uc-1',
        surface: 'node-designer',
        contract: 'frame_hook',
        prompt: 'Flip the frame vertically',
        current_code: HOOK_CODE,
      })
    );

    // The hook file now holds the accepted code…
    expect(sourceEditor(HOOK_PATH)).toHaveValue(GENERATED_CODE);

    // …and every sibling file is byte-identical to the scaffold.
    for (const path of NON_PY_PATHS) {
      openTab(container, path);
      expect(sourceEditor(path)).toHaveValue(SCAFFOLD_FILES[path]);
    }
  });
});

describe('GeneratePanel Code_Assistant integration', () => {
  it('renders the assistant on the .py tab and on no other generated tab (1.3)', async () => {
    authState.role = 'PortalAdmin';
    const container = await reachGeneratePanelSource();

    // Default active tab is the first sorted path (README.md): no panel.
    expect(sourceEditor('README.md')).toBeInTheDocument();
    expect(assistantPrompt()).toBeNull();

    openTab(container, HOOK_PATH);
    expect(sourceEditor(HOOK_PATH)).toHaveValue(HOOK_CODE);
    expect(assistantPrompt()).toBeInTheDocument();

    for (const path of ['meson.build', 'src/gstblurregions.c']) {
      openTab(container, path);
      expect(sourceEditor(path)).toBeInTheDocument();
      expect(assistantPrompt()).toBeNull();
    }
  });

  it('omits the assistant entry point for non-admin users (6.2, 6.5)', async () => {
    authState.role = 'Viewer';
    const container = await reachGeneratePanelSource();

    openTab(container, HOOK_PATH);
    expect(sourceEditor(HOOK_PATH)).toHaveValue(HOOK_CODE);
    expect(assistantPrompt()).toBeNull();
    expect(screen.queryByText('Code assistant')).toBeNull();
  });

  it('Accept replaces exactly the .py file in the files map (2.5)', async () => {
    const container = await reachGeneratePanelSource();
    openTab(container, HOOK_PATH);

    await assistAndAccept();

    expect(codeAssist).toHaveBeenCalledTimes(1);
    expect(codeAssist).toHaveBeenCalledWith(
      expect.objectContaining({
        usecase_id: 'uc-1',
        surface: 'node-designer',
        contract: 'frame_hook',
        current_code: HOOK_CODE,
      })
    );

    // The accepted code replaced the hook file — and, like a manual
    // edit, marked the generated source as edited.
    expect(sourceEditor(HOOK_PATH)).toHaveValue(GENERATED_CODE);
    expect(screen.getByText('Generated source (edited)')).toBeInTheDocument();

    for (const path of NON_PY_PATHS) {
      openTab(container, path);
      expect(sourceEditor(path)).toHaveValue(SCAFFOLD_FILES[path]);
    }
  });
});
