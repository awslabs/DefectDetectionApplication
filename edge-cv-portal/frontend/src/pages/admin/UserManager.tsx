/**
 * User Manager page (portal-user-manager Requirements 1.3, 1.4, 1.6,
 * 2.1-2.5).
 *
 * PortalAdmin-only tool for managing portal user accounts. The route is
 * registered inside the authenticated layout, so unauthenticated visitors
 * are redirected to /login before this component renders (Requirement 1.6).
 * Signed-in users without the PortalAdmin role see an access-denied notice
 * and no User Manager content (Requirement 1.4), mirroring the
 * BedrockConfigurationSettings "Portal Admin access required" pattern.
 *
 * The page lists every account in the Cognito user pool with client-side
 * filtering (Requirements 2.1-2.4). The accounts state is cleared before
 * each fetch and only repopulated on success, so a load failure renders an
 * error alert and never a partial or stale list (Requirement 2.5).
 *
 * Management actions (Requirements 3.x, 4.x, 5.x) are offered through the
 * table header's actions slot on the selected account: password change,
 * forgot-password, and role change modals (see UserManagerModals.tsx).
 * Successful actions surface a dismissible flashbar naming the account
 * (3.4) and re-fetch the account list (5.7). Below the account table, the
 * edge sync panel (UserManagerSyncPanel.tsx) shows the per-device account
 * sync status and offers the account multi-select sync action
 * (Requirements 7.1, 7.4).
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Flashbar,
  Header,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import type { FlashbarProps } from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import type { AdminAccount } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { getErrorMessage } from '../../utils/errorHandling';
import {
  ForgotPasswordModal,
  PasswordModal,
  RoleModal,
} from './UserManagerModals';
import UserManagerSyncPanel from './UserManagerSyncPanel';

/** Which action modal is open, if any. */
type ActiveModal = 'password' | 'forgot-password' | 'role' | null;

/** One display row of the accounts table (Requirement 2.1). */
export interface AccountRow {
  username: string;
  email: string;
  /** Portal_Role (default Viewer). */
  role: string;
  /** Cognito status, e.g. CONFIRMED, FORCE_CHANGE_PASSWORD. */
  status: string;
  enabled: boolean;
  enabledLabel: 'Enabled' | 'Disabled';
  edgeCapable: boolean;
  edgeCapableLabel: 'Yes' | 'No';
}

/**
 * Pure account filter (Requirements 2.2, 2.3, 2.4): case-insensitive
 * substring match on username or email. An empty or whitespace-only term
 * returns the full list; a term matching nothing returns an empty list
 * (the table renders its empty state, never an error).
 */
export function filterAccounts(
  accounts: AdminAccount[],
  term: string
): AdminAccount[] {
  const needle = term.trim().toLowerCase();
  if (!needle) {
    return accounts;
  }
  return accounts.filter(
    (account) =>
      (account.username ?? '').toLowerCase().includes(needle) ||
      (account.email ?? '').toLowerCase().includes(needle)
  );
}

/**
 * Pure table row-model builder (Requirement 2.1): exactly one row per
 * account, carrying the username, email, Portal_Role, Cognito status,
 * enabled/disabled state, and edge-login capability.
 */
export function buildAccountRows(accounts: AdminAccount[]): AccountRow[] {
  return accounts.map((account) => ({
    username: account.username,
    email: account.email,
    role: account.role || 'Viewer',
    status: account.user_status,
    enabled: account.enabled,
    enabledLabel: account.enabled ? 'Enabled' : 'Disabled',
    edgeCapable: account.edge_capable,
    edgeCapableLabel: account.edge_capable ? 'Yes' : 'No',
  }));
}

export default function UserManager() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';

  const [accounts, setAccounts] = useState<AdminAccount[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [filteringText, setFilteringText] = useState('');
  const [selectedUsername, setSelectedUsername] = useState<string | null>(null);
  const [activeModal, setActiveModal] = useState<ActiveModal>(null);
  const [flashItems, setFlashItems] = useState<FlashbarProps.MessageDefinition[]>([]);

  const loadAccounts = useCallback(async () => {
    // Clear the accounts state before every fetch so a failure can never
    // leave a partial or stale list on screen (Requirement 2.5).
    setAccounts([]);
    setError('');
    setLoading(true);
    try {
      const response = await apiService.listAdminUsers();
      setAccounts(response.users ?? []);
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to load user accounts'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isPortalAdmin) return;
    loadAccounts();
  }, [isPortalAdmin, loadAccounts]);

  /**
   * Shared success path for all three action modals: close the modal,
   * show a dismissible confirmation naming the account (Requirement 3.4),
   * and re-fetch the account list so changed attributes — e.g. a new
   * Portal_Role — are displayed (Requirement 5.7).
   */
  const pushFlash = (message: string) => {
    const id = `um-flash-${Date.now()}-${Math.random()}`;
    setFlashItems((items) => [
      ...items,
      {
        id,
        type: 'success',
        content: message,
        dismissible: true,
        onDismiss: () =>
          setFlashItems((current) => current.filter((item) => item.id !== id)),
      },
    ]);
  };

  const handleActionSuccess = (message: string) => {
    setActiveModal(null);
    pushFlash(message);
    loadAccounts();
  };

  if (!isPortalAdmin) {
    // Access-denied notice only — no User Manager content (Requirement 1.4).
    return (
      <Alert type="info" header="Portal Admin access required">
        User accounts can only be viewed and managed by Portal Admins.
      </Alert>
    );
  }

  const rows = buildAccountRows(filterAccounts(accounts, filteringText));
  const selectedRows = rows.filter((row) => row.username === selectedUsername);
  const selectedAccount =
    accounts.find((account) => account.username === selectedUsername) ?? null;

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Manage portal user accounts, passwords, and roles"
      >
        User Manager
      </Header>

      {flashItems.length > 0 && <Flashbar items={flashItems} />}

      {error ? (
        // A load failure replaces the account table entirely — never a
        // partial or stale list (Requirement 2.5).
        <Alert
          type="error"
          header="Failed to load user accounts"
          action={<Button onClick={loadAccounts}>Retry</Button>}
        >
          {error}
        </Alert>
      ) : (
        <Table
          header={
            <Header
              variant="h2"
              counter={`(${accounts.length})`}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" onClick={loadAccounts}>
                    Refresh
                  </Button>
                  <Button
                    disabled={!selectedAccount}
                    onClick={() => setActiveModal('password')}
                  >
                    Change password
                  </Button>
                  <Button
                    disabled={!selectedAccount}
                    onClick={() => setActiveModal('forgot-password')}
                  >
                    Send temporary password
                  </Button>
                  <Button
                    disabled={!selectedAccount}
                    onClick={() => setActiveModal('role')}
                  >
                    Change role
                  </Button>
                </SpaceBetween>
              }
            >
              Accounts
            </Header>
          }
          selectionType="single"
          selectedItems={selectedRows}
          onSelectionChange={({ detail }) =>
            setSelectedUsername(detail.selectedItems[0]?.username ?? null)
          }
          trackBy="username"
          ariaLabels={{
            selectionGroupLabel: 'Account selection',
            itemSelectionLabel: (_data, row) => `Select ${row.username}`,
          }}
          filter={
            <TextFilter
              filteringText={filteringText}
              onChange={({ detail }) => setFilteringText(detail.filteringText)}
              filteringPlaceholder="Find accounts by username or email"
              filteringAriaLabel="Filter accounts"
              countText={
                filteringText.trim()
                  ? `${rows.length} match${rows.length === 1 ? '' : 'es'}`
                  : ''
              }
            />
          }
          columnDefinitions={[
            {
              id: 'username',
              header: 'Username',
              cell: (item) => item.username,
            },
            {
              id: 'email',
              header: 'Email',
              cell: (item) => item.email,
            },
            {
              id: 'role',
              header: 'Role',
              cell: (item) => item.role,
            },
            {
              id: 'status',
              header: 'Status',
              cell: (item) => item.status,
            },
            {
              id: 'enabled',
              header: 'Enabled',
              cell: (item) => item.enabledLabel,
            },
            {
              id: 'edgeCapable',
              header: 'Edge login capable',
              cell: (item) => item.edgeCapableLabel,
            },
          ]}
          items={rows}
          loading={loading}
          loadingText="Loading accounts"
          variant="container"
          empty={
            // Empty state with no error message — shown both for an empty
            // pool and when a filter term matches nothing (Requirement 2.3).
            <Box textAlign="center" color="inherit">
              <b>No accounts</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                {filteringText.trim()
                  ? 'No accounts match the filter.'
                  : 'No accounts found.'}
              </Box>
            </Box>
          }
        />
      )}

      {selectedAccount && activeModal === 'password' && (
        <PasswordModal
          account={selectedAccount}
          onSuccess={handleActionSuccess}
          onDismiss={() => setActiveModal(null)}
        />
      )}
      {selectedAccount && activeModal === 'forgot-password' && (
        <ForgotPasswordModal
          account={selectedAccount}
          onSuccess={handleActionSuccess}
          onDismiss={() => setActiveModal(null)}
        />
      )}
      {selectedAccount && activeModal === 'role' && (
        <RoleModal
          account={selectedAccount}
          onSuccess={handleActionSuccess}
          onDismiss={() => setActiveModal(null)}
        />
      )}

      {/* Per-device account sync status and the account multi-select sync
          action (Requirements 7.1, 7.4). */}
      <UserManagerSyncPanel accounts={accounts} onSyncStarted={pushFlash} />
    </SpaceBetween>
  );
}
