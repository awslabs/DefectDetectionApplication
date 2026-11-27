/**
 * Tests for the Settings page's Auto-Labeling tab (Settings →
 * Auto-Labeling): a PortalAdmin sees the tab and opening it renders the
 * Auto-Labeling section; other roles do not get the tab.
 *
 * The settings sections are stubbed: their own component tests cover them.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import Settings from './Settings';

const { listDataAccounts, useAuthMock } = vi.hoisted(() => ({
  listDataAccounts: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', () => ({
  apiService: { listDataAccounts },
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

vi.mock('../components/BedrockConfigurationSettings', () => ({
  default: () => <div>BEDROCK_SETTINGS_STUB</div>,
}));
vi.mock('../components/BuildInfrastructureSettings', () => ({
  default: () => <div>BUILD_SETTINGS_STUB</div>,
}));
vi.mock('../components/assistant/AssistantSettings', () => ({
  default: () => <div>ASSISTANT_SETTINGS_STUB</div>,
}));
vi.mock('../components/AutoLabelingSettings', () => ({
  default: () => <div>AUTO_LABELING_SETTINGS_STUB</div>,
}));
vi.mock('../components/assistant/assistantApi', () => ({
  assistantConfigured: () => false,
}));

beforeEach(() => {
  vi.clearAllMocks();
  listDataAccounts.mockResolvedValue({ data_accounts: [] });
});

function setAuthRole(role: string) {
  useAuthMock.mockReturnValue({ user: { role } });
}

describe('Settings — the Auto-Labeling tab', () => {
  it('is listed for a PortalAdmin and renders the Auto-Labeling section when opened', async () => {
    setAuthRole('PortalAdmin');
    render(<Settings />);

    const tab = await screen.findByRole('tab', { name: 'Auto-Labeling' });
    expect(screen.queryByText('AUTO_LABELING_SETTINGS_STUB')).toBeNull();

    fireEvent.click(tab);

    expect(await screen.findByText('AUTO_LABELING_SETTINGS_STUB')).toBeInTheDocument();
  });

  it('is not listed for a DataScientist', async () => {
    setAuthRole('DataScientist');
    render(<Settings />);

    expect(await screen.findByRole('tab', { name: 'Data Accounts' })).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Auto-Labeling' })).toBeNull();
    expect(screen.queryByText('AUTO_LABELING_SETTINGS_STUB')).toBeNull();
  });
});
