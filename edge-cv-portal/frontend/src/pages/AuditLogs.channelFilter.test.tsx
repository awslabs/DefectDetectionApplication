/**
 * Component tests for the audit-log page's channel filter (mcp-servers
 * task 2.4, Requirement 4.6).
 *
 * Covers:
 * - the Channel select offers All / Web & API / MCP and starts on All, which
 *   sends no `channel` query parameter;
 * - choosing Web & API or MCP reloads the logs with `channel=api` or
 *   `channel=mcp`;
 * - an entry's `mcp_tool` is shown in the details column.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import AuditLogs from './AuditLogs';

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    listUseCases: vi.fn(),
    getAuditLogs: vi.fn(),
  },
}));

vi.mock('../services/api', () => ({ default: apiMocks }));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { user_id: 'u-1', username: 'admin', role: 'PortalAdmin' },
  }),
}));

vi.mock('../contexts/UsecaseContext', () => ({
  useUsecase: () => ({
    selectedUsecaseId: null,
    setSelectedUsecaseId: vi.fn(),
  }),
}));

const MCP_ENTRY = {
  event_id: 'user-1_1700000000000',
  timestamp: 1700000000000,
  user_id: 'user-1',
  usecase_id: 'uc-1',
  action: 'mcp_tool_call',
  resource_type: 'mcp_tool',
  resource_id: 'portal_devices_list',
  result: 'success',
  details: {},
  channel: 'mcp',
  mcp_tool: 'portal_devices_list',
};

function channelSelect() {
  const wrapper = createWrapper(document.body);
  const field = wrapper
    .findAllFormFields()
    .find(f => f.findLabel()?.getElement().textContent === 'Channel');
  if (!field) throw new Error('Channel form field not rendered');
  const select = field.findControl()?.findSelect();
  if (!select) throw new Error('Channel select not rendered');
  return select;
}

function lastChannelParam(): string | undefined {
  const calls = apiMocks.getAuditLogs.mock.calls;
  return calls[calls.length - 1][0]?.channel;
}

describe('AuditLogs channel filter', () => {
  beforeEach(() => {
    apiMocks.listUseCases.mockResolvedValue({ usecases: [] });
    apiMocks.getAuditLogs.mockResolvedValue({
      logs: [MCP_ENTRY],
      count: 1,
      scanned_count: 1,
      available_actions: ['mcp_tool_call'],
      is_admin: true,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('offers All, Web & API and MCP and starts on All without a channel parameter', async () => {
    render(<AuditLogs />);
    await waitFor(() => expect(apiMocks.getAuditLogs).toHaveBeenCalled());
    expect(lastChannelParam()).toBeUndefined();

    const select = channelSelect();
    expect(select.findTrigger().getElement().textContent).toContain('All channels');
    select.openDropdown();
    const labels = select
      .findDropdown()
      .findOptions()
      .map(o => o.getElement().textContent);
    expect(labels).toEqual(['All channels', 'Web & API', 'MCP']);
  });

  it('reloads with channel=mcp and channel=api when those options are chosen', async () => {
    render(<AuditLogs />);
    await waitFor(() => expect(apiMocks.getAuditLogs).toHaveBeenCalled());

    const select = channelSelect();
    select.openDropdown();
    select.selectOption(3);
    await waitFor(() => expect(lastChannelParam()).toBe('mcp'));

    select.openDropdown();
    select.selectOption(2);
    await waitFor(() => expect(lastChannelParam()).toBe('api'));

    select.openDropdown();
    select.selectOption(1);
    await waitFor(() => {
      const calls = apiMocks.getAuditLogs.mock.calls;
      expect(calls.length).toBeGreaterThanOrEqual(4);
      expect(lastChannelParam()).toBeUndefined();
    });
  });

  it('shows mcp_tool in the details column', async () => {
    render(<AuditLogs />);
    expect(await screen.findByText('mcp_tool: portal_devices_list')).toBeInTheDocument();
  });
});
