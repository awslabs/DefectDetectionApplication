/**
 * The assistant provider and drawer against a scripted API (spec
 * portal-ai-assistant; tasks.md 4.5). Requirements 1.1-1.6, 2.4-2.6, 3.1,
 * 3.9, 5.4, 8.1-8.2, 11.1, 11.4, 12.4.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { configMock, apiMock, useAuthMock } = vi.hoisted(() => ({
  configMock: { current: {} as Record<string, unknown> },
  apiMock: {
    state: vi.fn(), settings: vi.fn(), updateSettings: vi.fn(), listConversations: vi.fn(),
    createConversation: vi.fn(), getConversation: vi.fn(), deleteConversation: vi.fn(), startTurn: vi.fn(),
    getTurn: vi.fn(), confirm: vi.fn(), stop: vi.fn(),
  },
  useAuthMock: vi.fn(),
}));

vi.mock('../../config', () => ({
  getConfig: () => configMock.current,
  getBuildInfo: () => ({ version: 'test', gitSha: 'abc1234', buildTime: '' }),
}));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: useAuthMock }));
vi.mock('../../contexts/UsecaseContext', () => ({ useOptionalUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }) }));
vi.mock('./assistantApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./assistantApi')>();
  return {
    ...actual,
    assistantConfigured: () => typeof configMock.current.assistantApiUrl === 'string',
    assistantApi: apiMock,
  };
});

import { AssistantProvider, useAssistant } from './AssistantProvider';
import AssistantDrawer from './AssistantDrawer';
import { DRAWER_STATE_KEY } from './constants';

function Probe() {
  const assistant = useAssistant();
  return (
    <div>
      <span data-testid="available">{String(assistant.available)}</span>
      <span data-testid="open">{String(assistant.open)}</span>
      <span data-testid="unread">{String(assistant.unread)}</span>
      <button onClick={() => assistant.setOpen(!assistant.open)}>toggle</button>
      <button onClick={() => void assistant.retryState()}>refresh</button>
    </div>
  );
}

function mount(route = '/devices', children: ReactNode = <Probe />) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AssistantProvider>
        <Routes>
          <Route path="*" element={<>{children}</>} />
        </Routes>
      </AssistantProvider>
    </MemoryRouter>
  );
}

const user = { user_id: 'sub-1', email: 'alice@example.com', username: 'alice', role: 'UseCaseAdmin', is_super_user: false };

beforeEach(() => {
  configMock.current = { assistantApiUrl: 'https://assistant.example.test/v1', branding: {} };
  useAuthMock.mockReturnValue({ user });
  localStorage.clear();
  sessionStorage.clear();
  Object.defineProperty(window, 'innerWidth', { value: 1600, configurable: true });
  for (const fn of Object.values(apiMock)) fn.mockReset();
  apiMock.state.mockResolvedValue({ enabled: true, actions_enabled: false, version: '1.0.0' });
  apiMock.listConversations.mockResolvedValue({ items: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('availability (Requirements 1.5, 1.6, 12.4)', () => {
  it('is unavailable without an assistantApiUrl and makes no request', async () => {
    configMock.current = { branding: {} };
    mount();
    expect(screen.getByTestId('available').textContent).toBe('false');
    await act(async () => {});
    expect(apiMock.state).not.toHaveBeenCalled();
  });

  it('is unavailable for a DataLabeler-only user without asking the API', async () => {
    useAuthMock.mockReturnValue({ user: { ...user, role: 'DataLabeler' } });
    mount('/labeler');
    await act(async () => {});
    expect(screen.getByTestId('available').textContent).toBe('false');
    expect(apiMock.state).not.toHaveBeenCalled();
  });

  it('becomes unavailable when the state poll says disabled', async () => {
    mount();
    await waitFor(() => expect(screen.getByTestId('available').textContent).toBe('true'));
    apiMock.state.mockResolvedValue({ enabled: false, actions_enabled: false, reason: 'disabled_by_admin', version: '1.0.0' });
    // the 30 s state poll calls the same refresh the button does
    fireEvent.click(screen.getByText('refresh'));
    await waitFor(() => expect(screen.getByTestId('available').textContent).toBe('false'));
  });
});

describe('drawer state (Requirements 2.4, 2.5)', () => {
  it('opens on a wide first visit, stays closed on the builder, remembers the toggle', async () => {
    mount('/dashboard');
    expect(screen.getByTestId('open').textContent).toBe('true');
    fireEvent.click(screen.getByText('toggle'));
    expect(screen.getByTestId('open').textContent).toBe('false');
    expect(localStorage.getItem(DRAWER_STATE_KEY)).toBe('closed');
  });

  it('is closed on the builder before any toggle and open there once remembered', () => {
    const { unmount } = mount('/workflows/builder/wf-1');
    expect(screen.getByTestId('open').textContent).toBe('false');
    unmount();
    localStorage.setItem(DRAWER_STATE_KEY, 'open');
    mount('/workflows/builder/wf-1');
    expect(screen.getByTestId('open').textContent).toBe('true');
  });

  it('is closed on a narrow first visit', () => {
    Object.defineProperty(window, 'innerWidth', { value: 800, configurable: true });
    mount('/dashboard');
    expect(screen.getByTestId('open').textContent).toBe('false');
  });
});

describe('a turn through the drawer (Requirements 3.1, 5.3, 8.1, 8.2, 2.6)', () => {
  it('creates a conversation, starts the turn, polls, and folds the result into the log', async () => {
    apiMock.createConversation.mockResolvedValue({ conversation_id: '01ARZ3NDEKTSV4RRFFQ69G5FAV' });
    apiMock.startTurn.mockResolvedValue({ turn_id: '01ARZ3NDEKTSV4RRFFQ69G5FAW' });
    apiMock.getTurn
      .mockResolvedValueOnce({ turn_id: 't', conversation_id: 'c', status: 'running', version: 3, text: 'Looking',
        events: [{ seq: 1, ts: 0, type: 'tool_start', text_offset: 0, tool: 'portal_devices_list', title: 'List devices' }] })
      .mockResolvedValueOnce({ turn_id: 't', conversation_id: 'c', status: 'completed', version: 5, text: 'One device.', events: [] });
    apiMock.getConversation.mockResolvedValue({
      conversation_id: 'c', title: 'Which devices?', live_turn_id: null, turn_count: 1,
      messages: [
        { seq: 0, role: 'user', turn_id: 't', created_at: 1, blocks: [{ type: 'user', text: 'Which devices?' }] },
        { seq: 1, role: 'assistant', turn_id: 't', created_at: 2, blocks: [{ type: 'assistant', text: 'One device.', events: [], status: 'completed' }] },
      ],
    });
    mount('/devices', <AssistantDrawer />);
    await waitFor(() => expect(screen.getByPlaceholderText('Ask a question')).toBeInTheDocument());
    const textarea = screen.getByPlaceholderText('Ask a question') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'Which devices?' } });
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' });
    await waitFor(() => expect(apiMock.startTurn).toHaveBeenCalledTimes(1));
    expect(apiMock.startTurn.mock.calls[0][2]).toEqual({ route: '/devices', params: {}, usecase_id: 'uc-1' });
    // the live turn is visible with its prompt and, after the first poll, its text and trace
    expect(screen.getByTestId('assistant-live-turn')).toBeInTheDocument();
    expect(screen.getByTestId('assistant-stop')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Looking')).toBeInTheDocument(), { timeout: 3000 });
    expect(screen.getByTestId('assistant-trace').textContent).toContain('List devices');
    // the second poll ends the turn: the stored messages replace the live view
    await waitFor(() => expect(screen.getAllByTestId('assistant-message-assistant')).toHaveLength(1), { timeout: 4000 });
    expect(screen.queryByTestId('assistant-live-turn')).toBeNull();
    // the bubble and the hidden live region both carry the final text
    expect(screen.getAllByText('One device.').length).toBeGreaterThanOrEqual(1);
  });

  it('refuses a whitespace prompt without calling the API', async () => {
    mount('/devices', <AssistantDrawer />);
    await waitFor(() => expect(screen.getByPlaceholderText('Ask a question')).toBeInTheDocument());
    const textarea = screen.getByPlaceholderText('Ask a question') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: '   ' } });
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' });
    await act(async () => {});
    expect(apiMock.createConversation).not.toHaveBeenCalled();
    expect(apiMock.startTurn).not.toHaveBeenCalled();
  });

  it('shows the confirmation card with every argument and the use case, and sends the decision', async () => {
    apiMock.createConversation.mockResolvedValue({ conversation_id: 'c' });
    apiMock.startTurn.mockResolvedValue({ turn_id: 't' });
    apiMock.getTurn.mockResolvedValue({
      turn_id: 't', conversation_id: 'c', status: 'awaiting_confirmation', version: 4, text: '', events: [],
      confirmation: {
        binding_id: 'b1', tool: 'portal_devices_deployment_create', title: 'Create deployment',
        description: 'Deploys components.', args: { usecase_id: 'uc-1', target_thing_group: 'line-1' },
        usecase_id: 'uc-1', destructive: true, expires_at: Math.floor(Date.now() / 1000) + 600,
      },
    });
    apiMock.confirm.mockResolvedValue({ turn_id: 't', decision: 'confirm' });
    mount('/deployments', <AssistantDrawer />);
    await waitFor(() => expect(screen.getByPlaceholderText('Ask a question')).toBeInTheDocument());
    const textarea = screen.getByPlaceholderText('Ask a question') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'Deploy to line-1' } });
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' });
    await waitFor(() => expect(screen.getByTestId('assistant-confirmation')).toBeInTheDocument(), { timeout: 3000 });
    const args = screen.getByTestId('assistant-confirmation-args');
    expect(args.textContent).toContain('target_thing_group');
    expect(args.textContent).toContain('line-1');
    expect(args.textContent).toContain('usecase_id');
    expect(screen.getByTestId('assistant-confirmation-usecase').textContent).toBe('uc-1');
    expect(screen.getByText('DESTRUCTIVE')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('assistant-confirm'));
    await waitFor(() => expect(apiMock.confirm).toHaveBeenCalledWith('c', 't', 'b1', 'confirm'));
  });

  it('shows a failed turn with its category and keeps the page usable', async () => {
    apiMock.createConversation.mockResolvedValue({ conversation_id: 'c' });
    apiMock.startTurn.mockRejectedValue(Object.assign(new Error('the tokens per day limit is reached'),
      { code: 'quota_exceeded', details: { limit: 'tokens_per_day', resets_at: '2026-11-04T00:00:00Z' } }));
    mount('/devices', <AssistantDrawer />);
    await waitFor(() => expect(screen.getByPlaceholderText('Ask a question')).toBeInTheDocument());
    const textarea = screen.getByPlaceholderText('Ask a question') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'hello' } });
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' });
    await waitFor(() => expect(screen.getByText(/tokens per day limit/)).toBeInTheDocument());
    expect((screen.getByPlaceholderText('Ask a question') as HTMLTextAreaElement).value).toBe('hello');
  });
});
