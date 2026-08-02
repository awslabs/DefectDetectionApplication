/**
 * Component tests for the prompt-based generation panel
 * (custom-node-designer task 12.7, Requirements 2.3, 2.6): submitting a
 * prompt starts an asynchronous generation turn (202) which the panel
 * polls until it settles; the generated Plugin_Scaffold source is
 * displayed for review and optional editing before acceptance (2.3);
 * failed turns display the error and preserve the prompt in the input
 * box for retry (2.6). The panel polls immediately after the start
 * response, so mocked terminal poll states resolve without advancing
 * timers.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import GeneratePanel from './GeneratePanel';

const {
  navigateMock,
  listUseCases,
  startGeneration,
  continueGeneration,
  getGenerationTurn,
  acceptGeneration,
  putVersionSource,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  startGeneration: vi.fn(),
  continueGeneration: vi.fn(),
  getGenerationTurn: vi.fn(),
  acceptGeneration: vi.fn(),
  putVersionSource: vi.fn(),
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
  return { ApiError, apiService: { listUseCases } };
});

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'u-1',
      email: 'user@example.com',
      username: 'user',
      role: 'UseCaseAdmin',
      is_super_user: false,
    },
  }),
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    startGeneration,
    continueGeneration,
    getGenerationTurn,
    acceptGeneration,
    putVersionSource,
  },
}));

/** 202 body of the start routes: the turn runs asynchronously. */
const STARTED = {
  session_id: 's-1',
  usecase_id: 'uc-1',
  turn_status: 'pending',
};

/** Completed poll state carrying the generated source (2.3). */
const COMPLETED_TURN = {
  session_id: 's-1',
  usecase_id: 'uc-1',
  turn_status: 'completed',
  files: { 'python/blur_hook.py': 'def process_frame(frame, params):\n    return frame\n' },
  assistant_text: 'Generated a blur node scaffold.',
  model_id: 'model-1',
};

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
});

async function fillSetupAndPrompt(container: HTMLElement, prompt: string) {
  await waitFor(() => expect(listUseCases).toHaveBeenCalled());
  const nameInput = container.querySelector('input[placeholder="Blur Regions"]')!;
  fireEvent.change(nameInput, { target: { value: 'Blur Regions' } });
  const promptBox = screen.getByLabelText('Node behavior prompt');
  fireEvent.change(promptBox, { target: { value: prompt } });
}

describe('GeneratePanel', () => {
  it('displays the error and preserves the prompt when the polled turn fails (2.6)', async () => {
    startGeneration.mockResolvedValue(STARTED);
    getGenerationTurn.mockResolvedValue({
      session_id: 's-1',
      usecase_id: 'uc-1',
      turn_status: 'failed',
      turn_error: {
        code: 'GENERATED_SCAFFOLD_INVALID',
        message: 'Generated source failed scaffold validation',
        details: { defects: ['missing meson.build for x86_64'] },
        http_status: 422,
      },
    });
    const { container } = render(<GeneratePanel />);
    await fillSetupAndPrompt(container, 'Blur every region in each frame');

    fireEvent.click(screen.getByRole('button', { name: 'Generate scaffold' }));

    // The failure arrives through the poll and is displayed with its
    // scaffold-validation defects.
    await screen.findByText('Generated source is not a buildable Plugin_Scaffold');
    expect(getGenerationTurn).toHaveBeenCalledWith('s-1');
    expect(screen.getByText('missing meson.build for x86_64')).toBeInTheDocument();
    expect(
      screen.getByText('Your prompt is preserved below — adjust it and retry.')
    ).toBeInTheDocument();

    // The prompt stays in the input box for retry (2.6).
    expect(screen.getByLabelText('Node behavior prompt')).toHaveValue(
      'Blur every region in each frame'
    );
    // No completed turn: still the setup phase.
    expect(screen.queryByText('Generated source')).toBeNull();
  });

  it('polls the started turn and shows the generated source for review and editing (2.3)', async () => {
    startGeneration.mockResolvedValue(STARTED);
    getGenerationTurn.mockResolvedValue(COMPLETED_TURN);
    acceptGeneration.mockResolvedValue({ plugin: { plugin_id: 'p-9', version: 1 } });
    putVersionSource.mockResolvedValue({ files: ['python/blur_hook.py'], count: 1 });

    const { container } = render(<GeneratePanel />);
    await fillSetupAndPrompt(container, 'Blur every region in each frame');
    fireEvent.click(screen.getByRole('button', { name: 'Generate scaffold' }));

    // Chat phase after the poll completes: transcript plus the generated
    // source for review.
    await screen.findByText('Generated source');
    expect(startGeneration).toHaveBeenCalledWith({
      usecase_id: 'uc-1',
      prompt: 'Blur every region in each frame',
      declaration: expect.anything(),
    });
    expect(getGenerationTurn).toHaveBeenCalledWith('s-1');
    expect(screen.getByText('Generated a blur node scaffold.')).toBeInTheDocument();
    const editor = screen.getByLabelText('Source of python/blur_hook.py');
    expect(editor).toHaveValue(COMPLETED_TURN.files['python/blur_hook.py']);

    // The prompt box cleared on success and offers follow-ups.
    expect(screen.getByLabelText('Node behavior prompt')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Send follow-up' })).toBeInTheDocument();

    // Optional editing before acceptance (2.3).
    fireEvent.change(editor, {
      target: { value: 'def process_frame(frame, params):\n    return None\n' },
    });
    expect(screen.getByText('Generated source (edited)')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Accept and create plugin record' }));
    await waitFor(() => expect(acceptGeneration).toHaveBeenCalledWith('s-1', {
      name: 'Blur Regions',
      description: undefined,
    }));
    // The local edits are persisted over the accepted snapshot.
    await waitFor(() =>
      expect(putVersionSource).toHaveBeenCalledWith('p-9', 1, {
        'python/blur_hook.py': 'def process_frame(frame, params):\n    return None\n',
      })
    );
    expect(navigateMock).toHaveBeenCalledWith('/node-designer/plugins/p-9');
  });
});
