/**
 * Regression tests: the audit-log page keeps the selected Channel and Use Case
 * on every fetch, and the table always shows the result of the most recent
 * request.
 *
 * Each GET /audit-logs is a separate scan, so responses can arrive in any
 * order. These tests hold every getAuditLogs promise and resolve them out of
 * order to cover:
 * - the reload caused by listUseCases resolving (saved use case) after MCP is
 *   chosen still sends channel=mcp, and the earlier unfiltered response does
 *   not replace the MCP row;
 * - an earlier response that resolves after a later one is ignored;
 * - choosing "All Use Cases" fetches and renders rows instead of leaving the
 *   table empty.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import AuditLogs from './AuditLogs';
import { UsecaseProvider } from '../contexts/UsecaseContext';

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

const USECASE_STORAGE_KEY = 'dda-selected-usecase-id';
const USECASE = { usecase_id: 'uc-1', name: 'Line A' };

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

const WEB_ENTRY = {
  event_id: 'user-1_1700000001000',
  timestamp: 1700000001000,
  user_id: 'user-1',
  usecase_id: 'uc-1',
  action: 'list_usecases',
  resource_type: 'usecase',
  resource_id: 'web-resource-1',
  result: 'success',
  details: {},
};

type AuditParams = { channel?: string; usecase_id?: string; next_token?: string };

// What the scan returns for a request: MCP rows for channel=mcp, nothing on
// the first page of a use-case filter, Web rows otherwise.
function responseFor(params: AuditParams) {
  let logs: object[];
  if (params.channel === 'mcp') logs = [MCP_ENTRY];
  else if (params.usecase_id) logs = [];
  else logs = [WEB_ENTRY];
  return { logs, count: logs.length, scanned_count: 50, available_actions: [], is_admin: true };
}

interface PendingCall {
  params: AuditParams;
  resolve: () => Promise<void>;
}

let pending: PendingCall[] = [];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => {
    resolve = r;
  });
  return { promise, resolve };
}

function holdAuditLogs() {
  apiMocks.getAuditLogs.mockImplementation((params: AuditParams) => {
    const d = deferred<ReturnType<typeof responseFor>>();
    const call: PendingCall = {
      params,
      resolve: async () => {
        await act(async () => {
          d.resolve(responseFor(params));
        });
      },
    };
    pending.push(call);
    return d.promise;
  });
}

function selectByLabel(label: string) {
  const field = createWrapper(document.body)
    .findAllFormFields()
    .find(f => f.findLabel()?.getElement().textContent === label);
  const select = field?.findControl()?.findSelect();
  if (!select) throw new Error(`${label} select not rendered`);
  return select;
}

function choose(label: string, optionIndex: number) {
  const select = selectByLabel(label);
  select.openDropdown();
  select.selectOption(optionIndex);
}

function tableText(): string {
  return createWrapper(document.body).findTable()?.getElement().textContent ?? '';
}

function lastParams(): AuditParams {
  const calls = apiMocks.getAuditLogs.mock.calls;
  return calls[calls.length - 1][0];
}

function renderPage() {
  return render(
    <UsecaseProvider>
      <AuditLogs />
    </UsecaseProvider>
  );
}

describe('AuditLogs filter retention', () => {
  beforeEach(() => {
    pending = [];
    holdAuditLogs();
  });

  afterEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('keeps channel=mcp on the reload caused by listUseCases resolving and shows the MCP row', async () => {
    localStorage.setItem(USECASE_STORAGE_KEY, USECASE.usecase_id);
    const useCases = deferred<{ usecases: typeof USECASE[] }>();
    apiMocks.listUseCases.mockReturnValue(useCases.promise);

    renderPage();
    await waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0].params.channel).toBeUndefined();

    choose('Channel', 3);
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].params.channel).toBe('mcp');

    // The saved use case is applied only once listUseCases resolves.
    await act(async () => {
      useCases.resolve({ usecases: [USECASE] });
    });
    await waitFor(() => expect(pending).toHaveLength(3));
    expect(lastParams()).toMatchObject({ channel: 'mcp', usecase_id: 'uc-1' });

    // Newest first; the initial unfiltered scan is the slowest.
    await pending[2].resolve();
    await pending[1].resolve();
    await pending[0].resolve();

    expect(lastParams()).toMatchObject({ channel: 'mcp', usecase_id: 'uc-1' });
    expect(selectByLabel('Channel').findTrigger().getElement().textContent).toContain('MCP');
    expect(tableText()).toContain('mcp_tool: portal_devices_list');
    expect(tableText()).not.toContain('web-resource-1');
  });

  it('ignores an earlier response that resolves after a later one', async () => {
    apiMocks.listUseCases.mockResolvedValue({ usecases: [] });

    renderPage();
    await waitFor(() => expect(pending).toHaveLength(1));

    choose('Channel', 3);
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].params.channel).toBe('mcp');

    await pending[1].resolve();
    expect(tableText()).toContain('mcp_tool: portal_devices_list');

    await pending[0].resolve();
    expect(tableText()).toContain('mcp_tool: portal_devices_list');
    expect(tableText()).not.toContain('web-resource-1');
  });

  it('fetches and renders rows when All Use Cases is chosen', async () => {
    localStorage.setItem(USECASE_STORAGE_KEY, USECASE.usecase_id);
    apiMocks.listUseCases.mockResolvedValue({ usecases: [USECASE] });

    renderPage();
    // Initial load, then the reload for the saved use case.
    await waitFor(() => expect(lastParams()?.usecase_id).toBe('uc-1'));
    await pending[0].resolve();

    const callsBefore = apiMocks.getAuditLogs.mock.calls.length;
    choose('Use Case', 1);
    await waitFor(() =>
      expect(apiMocks.getAuditLogs.mock.calls.length).toBeGreaterThan(callsBefore)
    );
    expect(lastParams().usecase_id).toBeUndefined();

    // The All Use Cases scan answers first; the use-case scan (an empty first
    // page) arrives after it.
    const allCall = pending[pending.length - 1];
    await allCall.resolve();
    for (const call of pending.slice(1, -1)) await call.resolve();

    expect(selectByLabel('Use Case').findTrigger().getElement().textContent).toContain('All Use Cases');
    expect(tableText()).toContain('web-resource-1');
    expect(tableText()).not.toContain('No audit logs');
  });
});
