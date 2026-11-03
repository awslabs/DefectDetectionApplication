/**
 * The assistant Settings tab against a scripted API (spec portal-ai-assistant
 * Requirement 12; tasks.md 4.5): PortalAdmin-only, the on/off toggle, the
 * Actions_Switch hidden until actions are delivered, retention validation,
 * and the save round trip.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { apiMock, useAuthMock } = vi.hoisted(() => ({
  apiMock: { settings: vi.fn(), updateSettings: vi.fn() },
  useAuthMock: vi.fn(),
}));

vi.mock('../../contexts/AuthContext', () => ({ useAuth: useAuthMock }));
vi.mock('./assistantApi', () => ({ assistantApi: apiMock }));

import AssistantSettings from './AssistantSettings';
import type { AssistantSettingsView } from './assistantApi';

const view: AssistantSettingsView = {
  enabled: true,
  actions_enabled: false,
  actions_delivered: false,
  retention_days: 30,
  guardrail_id: null,
  guardrail_version: null,
  model: { model_id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', region: 'us-east-1', tool_use_supported: null, checked_at: null },
  limits: { turns_per_hour: 60, concurrent_turns: 2 },
  version: '1.0.0',
};

const admin = { user_id: 'sub-admin', email: 'admin@example.com', username: 'admin', role: 'PortalAdmin', is_super_user: true };
const useCaseAdmin = { ...admin, user_id: 'sub-uca', role: 'UseCaseAdmin', is_super_user: false };

/** The Cloudscape Toggle renders a native checkbox inside the element carrying data-testid. */
function toggleInput(testId: string): HTMLInputElement {
  return screen.getByTestId(testId).querySelector('input[type="checkbox"]') as HTMLInputElement;
}

beforeEach(() => {
  apiMock.settings.mockReset();
  apiMock.updateSettings.mockReset();
  useAuthMock.mockReturnValue({ user: admin });
  apiMock.settings.mockResolvedValue(view);
});

describe('AssistantSettings', () => {
  it('shows only a notice to a non-PortalAdmin and never reads the settings (12.1)', () => {
    useAuthMock.mockReturnValue({ user: useCaseAdmin });
    render(<AssistantSettings />);
    expect(screen.getByText('Only PortalAdmins can change the assistant settings.')).toBeInTheDocument();
    expect(apiMock.settings).not.toHaveBeenCalled();
  });

  it('loads the record, hides the Actions switch until actions are delivered, and lists the limits', async () => {
    render(<AssistantSettings />);
    await waitFor(() => expect(toggleInput('assistant-enabled-toggle')).toBeChecked());
    expect(screen.queryByTestId('assistant-actions-toggle')).toBeNull();
    expect(screen.getByText('turns_per_hour')).toBeInTheDocument();
    expect(screen.getByText('Tool use not checked yet')).toBeInTheDocument();
    expect((screen.getByTestId('assistant-retention').querySelector('input') as HTMLInputElement).value).toBe('30');
  });

  it('shows the Actions switch once actions are delivered (Phase 2 switch, 12.5)', async () => {
    apiMock.settings.mockResolvedValue({ ...view, actions_delivered: true, actions_enabled: true });
    render(<AssistantSettings />);
    await waitFor(() => expect(toggleInput('assistant-actions-toggle')).toBeChecked());
  });

  it('turning the assistant off sends {enabled: false} and confirms it (12.2, 12.6)', async () => {
    apiMock.updateSettings.mockResolvedValue({ ...view, enabled: false, updated_by: 'admin', updated_at: 1_700_000_000_000 });
    render(<AssistantSettings />);
    await waitFor(() => expect(toggleInput('assistant-enabled-toggle')).toBeChecked());
    fireEvent.click(toggleInput('assistant-enabled-toggle'));
    await waitFor(() => expect(apiMock.updateSettings).toHaveBeenCalledWith({ enabled: false }));
    await waitFor(() => expect(screen.getByText('Assistant disabled')).toBeInTheDocument());
    expect(toggleInput('assistant-enabled-toggle')).not.toBeChecked();
    expect(screen.getByText(/Last changed .* by admin/)).toBeInTheDocument();
  });

  it('refuses a retention outside 1-365 before any request, then saves a valid one with the guardrail (12.3)', async () => {
    apiMock.updateSettings.mockResolvedValue({ ...view, retention_days: 90, guardrail_id: 'abc123', guardrail_version: 'DRAFT' });
    render(<AssistantSettings />);
    await waitFor(() => expect(screen.getByTestId('assistant-retention')).toBeInTheDocument());
    const retention = screen.getByTestId('assistant-retention').querySelector('input') as HTMLInputElement;
    fireEvent.change(retention, { target: { value: '400' } });
    expect(screen.getByText('Enter a whole number between 1 and 365')).toBeInTheDocument();
    const save = screen.getByRole('button', { name: 'Save' });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(apiMock.updateSettings).not.toHaveBeenCalled();

    fireEvent.change(retention, { target: { value: '90' } });
    fireEvent.change(screen.getByPlaceholderText('none'), { target: { value: 'abc123' } });
    fireEvent.change(screen.getByPlaceholderText('DRAFT'), { target: { value: 'DRAFT' } });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(apiMock.updateSettings).toHaveBeenCalledWith({
      retention_days: 90, guardrail_id: 'abc123', guardrail_version: 'DRAFT',
    }));
    await waitFor(() => expect(screen.getByText('Assistant settings saved')).toBeInTheDocument());
  });

  it('surfaces a failed write and keeps the previous value', async () => {
    apiMock.updateSettings.mockRejectedValue(new Error('Forbidden'));
    render(<AssistantSettings />);
    await waitFor(() => expect(toggleInput('assistant-enabled-toggle')).toBeChecked());
    fireEvent.click(toggleInput('assistant-enabled-toggle'));
    await waitFor(() => expect(screen.getByText('Forbidden')).toBeInTheDocument());
    expect(toggleInput('assistant-enabled-toggle')).toBeChecked();
  });
});
