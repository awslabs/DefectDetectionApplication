/**
 * Example-based unit tests for the User Manager page
 * (portal-user-manager Requirements 1.4, 2.1-2.5):
 *
 * - `filterAccounts`: case-insensitive substring filter on username or
 *   email; empty/whitespace term returns the full list; no match returns
 *   an empty list (2.2, 2.3, 2.4).
 * - `buildAccountRows`: one row per account carrying username, email,
 *   role, Cognito status, enabled/disabled, and edge capability (2.1).
 * - Rendering: non-PortalAdmin users see the access-denied notice and no
 *   account content (1.4); PortalAdmin sees the populated table (2.1);
 *   a load failure renders an error alert and never a partial list (2.5).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import UserManager, { buildAccountRows, filterAccounts } from './UserManager';
import type { AdminAccount } from '../../services/api';

const {
  listAdminUsers,
  listEdgeSyncDevices,
  syncEdgeDevice,
  deleteAdminUser,
  useAuthMock,
} = vi.hoisted(() => ({
  listAdminUsers: vi.fn(),
  listEdgeSyncDevices: vi.fn(),
  syncEdgeDevice: vi.fn(),
  deleteAdminUser: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    listAdminUsers,
    listEdgeSyncDevices,
    syncEdgeDevice,
    deleteAdminUser,
  },
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

function account(overrides: Partial<AdminAccount> = {}): AdminAccount {
  return {
    username: 'operator1',
    email: 'op1@example.com',
    email_verified: true,
    role: 'Operator',
    user_status: 'CONFIRMED',
    enabled: true,
    edge_capable: false,
    ...overrides,
  };
}

const ACCOUNTS: AdminAccount[] = [
  account(),
  account({
    username: 'admin',
    email: 'admin@example.com',
    role: 'PortalAdmin',
    user_status: 'CONFIRMED',
    edge_capable: true,
  }),
  account({
    username: 'viewer-x',
    email: 'x@other.org',
    role: 'Viewer',
    user_status: 'FORCE_CHANGE_PASSWORD',
    enabled: false,
  }),
];

function setAuthRole(role: string | null) {
  useAuthMock.mockReturnValue({ user: role ? { role } : null });
}

beforeEach(() => {
  vi.clearAllMocks();
  listAdminUsers.mockResolvedValue({ users: ACCOUNTS, total_count: ACCOUNTS.length });
  listEdgeSyncDevices.mockResolvedValue({ devices: [], count: 0 });
});

describe('filterAccounts', () => {
  it('returns the full list for an empty term (Requirement 2.4)', () => {
    expect(filterAccounts(ACCOUNTS, '')).toEqual(ACCOUNTS);
  });

  it('returns the full list for a whitespace-only term', () => {
    expect(filterAccounts(ACCOUNTS, '   ')).toEqual(ACCOUNTS);
  });

  it('matches a case-insensitive substring of the username (Requirement 2.2)', () => {
    const result = filterAccounts(ACCOUNTS, 'ADMIN');
    expect(result.map((a) => a.username)).toEqual(['admin']);
  });

  it('matches a case-insensitive substring of the email (Requirement 2.2)', () => {
    const result = filterAccounts(ACCOUNTS, 'Other.ORG');
    expect(result.map((a) => a.username)).toEqual(['viewer-x']);
  });

  it('keeps every account whose username or email contains the term', () => {
    // "op" is in "operator1"/"op1@example.com" only.
    const result = filterAccounts(ACCOUNTS, 'example.com');
    expect(result.map((a) => a.username)).toEqual(['operator1', 'admin']);
  });

  it('returns an empty list when nothing matches (Requirement 2.3)', () => {
    expect(filterAccounts(ACCOUNTS, 'no-such-user')).toEqual([]);
  });

  it('handles an empty account list', () => {
    expect(filterAccounts([], 'anything')).toEqual([]);
  });
});

describe('buildAccountRows', () => {
  it('produces exactly one row per account with all display fields (Requirement 2.1)', () => {
    const rows = buildAccountRows(ACCOUNTS);
    expect(rows).toHaveLength(ACCOUNTS.length);
    expect(rows[0]).toEqual({
      username: 'operator1',
      email: 'op1@example.com',
      role: 'Operator',
      status: 'CONFIRMED',
      enabled: true,
      enabledLabel: 'Enabled',
      edgeCapable: false,
      edgeCapableLabel: 'No',
    });
  });

  it('labels disabled accounts and edge-capable accounts', () => {
    const rows = buildAccountRows(ACCOUNTS);
    const admin = rows.find((r) => r.username === 'admin')!;
    expect(admin.edgeCapableLabel).toBe('Yes');
    expect(admin.enabledLabel).toBe('Enabled');

    const disabled = rows.find((r) => r.username === 'viewer-x')!;
    expect(disabled.enabledLabel).toBe('Disabled');
    expect(disabled.status).toBe('FORCE_CHANGE_PASSWORD');
  });

  it('defaults a missing role to Viewer', () => {
    const rows = buildAccountRows([account({ role: '' })]);
    expect(rows[0].role).toBe('Viewer');
  });

  it('returns an empty row list for no accounts', () => {
    expect(buildAccountRows([])).toEqual([]);
  });
});

describe('UserManager rendering', () => {
  it.each(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin'])(
    'shows the access-denied notice and nothing else for %s (Requirement 1.4)',
    (role) => {
      setAuthRole(role);
      render(<UserManager />);

      expect(screen.getByText('Portal Admin access required')).toBeInTheDocument();
      expect(screen.queryByText('User Manager')).toBeNull();
      expect(screen.queryByRole('table')).toBeNull();
      expect(listAdminUsers).not.toHaveBeenCalled();
    }
  );

  it('shows the access-denied notice when no user is available', () => {
    setAuthRole(null);
    render(<UserManager />);

    expect(screen.getByText('Portal Admin access required')).toBeInTheDocument();
    expect(listAdminUsers).not.toHaveBeenCalled();
  });

  it('lists every account with its details for PortalAdmin (Requirement 2.1)', async () => {
    setAuthRole('PortalAdmin');
    render(<UserManager />);

    await waitFor(() => {
      expect(screen.getByText('operator1')).toBeInTheDocument();
    });
    expect(listAdminUsers).toHaveBeenCalledTimes(1);

    // Every account appears with its email, role, status, and states.
    expect(screen.getByText('admin@example.com')).toBeInTheDocument();
    expect(screen.getByText('x@other.org')).toBeInTheDocument();
    expect(screen.getByText('PortalAdmin')).toBeInTheDocument();
    expect(screen.getByText('FORCE_CHANGE_PASSWORD')).toBeInTheDocument();
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    expect(screen.getByText('Yes')).toBeInTheDocument();

    // All six columns are present (Requirement 2.1).
    for (const header of ['Username', 'Email', 'Role', 'Status', 'Enabled', 'Edge login capable']) {
      expect(screen.getByRole('columnheader', { name: header })).toBeInTheDocument();
    }
  });

  it('enables the action buttons only once an account is selected and opens the matching modal', async () => {
    setAuthRole('PortalAdmin');
    render(<UserManager />);
    await waitFor(() => {
      expect(screen.getByText('operator1')).toBeInTheDocument();
    });

    const isDisabled = (button: HTMLElement) =>
      button.hasAttribute('disabled') ||
      button.getAttribute('aria-disabled') === 'true';

    const actionNames = ['Change password', 'Send temporary password', 'Change role'];
    for (const name of actionNames) {
      expect(isDisabled(screen.getByRole('button', { name }))).toBe(true);
    }

    // Select a row: all three actions become available.
    fireEvent.click(screen.getByRole('radio', { name: 'Select operator1' }));
    for (const name of actionNames) {
      expect(isDisabled(screen.getByRole('button', { name }))).toBe(false);
    }

    // Opening an action shows its modal for the selected account.
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
    expect(
      screen.getByText('Change password for operator1')
    ).toBeInTheDocument();
  });

  it('deletes the selected account and re-fetches the list with a confirmation (Requirement 14.7)', async () => {
    setAuthRole('PortalAdmin');
    deleteAdminUser.mockResolvedValue({ message: 'deleted' });
    render(<UserManager />);
    await waitFor(() => {
      expect(screen.getByText('operator1')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('radio', { name: 'Select operator1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    // Explicit confirmation naming the account (Requirement 14.1).
    expect(screen.getByText('Delete account operator1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Delete account' }));

    await waitFor(() =>
      expect(deleteAdminUser).toHaveBeenCalledWith('operator1')
    );
    // Success flashbar identifies the deleted account and the list is
    // re-fetched without it (Requirement 14.7).
    expect(
      await screen.findByText('Account operator1 was deleted.')
    ).toBeInTheDocument();
    expect(listAdminUsers).toHaveBeenCalledTimes(2);
  });

  it('shows a not-found error in a flashbar and refreshes the list (Requirement 14.11)', async () => {
    setAuthRole('PortalAdmin');
    deleteAdminUser.mockRejectedValue(
      Object.assign(new Error('User not found'), { status: 404 })
    );
    render(<UserManager />);
    await waitFor(() => {
      expect(screen.getByText('operator1')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('radio', { name: 'Select operator1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete account' }));

    // The error is surfaced in a flashbar and the account list is
    // refreshed (Requirement 14.11).
    expect(
      await screen.findByText(/operator1 was not found/)
    ).toBeInTheDocument();
    expect(listAdminUsers).toHaveBeenCalledTimes(2);
    // The confirmation modal is closed.
    expect(screen.queryByText('Delete account operator1')).toBeNull();
  });

  it('renders an error alert and no account rows on load failure (Requirement 2.5)', async () => {
    setAuthRole('PortalAdmin');
    listAdminUsers.mockRejectedValue(new Error('backend exploded'));
    render(<UserManager />);

    expect(await screen.findByText('Failed to load user accounts')).toBeInTheDocument();
    expect(screen.getByText('backend exploded')).toBeInTheDocument();
    // Never a partial or stale list: the accounts table is replaced
    // entirely (only the edge sync panel's device table remains).
    expect(screen.queryByText('Accounts')).toBeNull();
    expect(screen.queryByText('operator1')).toBeNull();
  });
});
