import { useState } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import {
  AppLayout,
  TopNavigation,
  SideNavigation,
  SideNavigationProps,
  ButtonDropdownProps,
  Modal,
  SpaceBetween,
  FormField,
  Input,
  Button,
  Box,
  Alert,
} from '@cloudscape-design/components';
import { useAuth } from '../contexts/AuthContext';
import { UserRole } from '../types';
import { getConfig, getBuildInfo } from '../config';
import { canAccessBuilds } from '../utils/buildsAccess';

/**
 * Builds the items for the top-navigation settings dropdown based on the
 * user's role (portal-user-manager Requirements 1.1, 1.2).
 *
 * Exported as a pure function so the role gating is directly testable:
 * the `user-manager` item is present only for PortalAdmin users — it is
 * omitted entirely (not merely disabled) for every other role.
 */
export function buildSettingsDropdownItems(
  role: UserRole | undefined
): ButtonDropdownProps.ItemOrGroup[] {
  const isPortalAdmin = role === 'PortalAdmin';
  return [
    {
      id: 'profile',
      text: 'Profile',
      disabled: true,
    },
    {
      id: 'settings',
      text: 'Settings',
      // The settings page is PortalAdmin-only (same rule as the
      // sidebar link); other roles see the item disabled.
      disabled: !isPortalAdmin,
    },
    // The User Manager tool is PortalAdmin-only and, unlike the settings
    // item, is omitted entirely for other roles (Requirement 1.2).
    ...(isPortalAdmin
      ? [{ id: 'user-manager', text: 'User Manager' }]
      : []),
    {
      id: 'change-password',
      text: 'Change Password',
    },
    {
      id: 'logout',
      text: 'Sign out',
    },
  ];
}

/**
 * Builds the side-navigation item list for a user's role
 * (build-fleet-rbac-visibility Requirements 2.5, 2.6, 3.6).
 *
 * Exported as a pure function so the role gating is directly property-testable
 * (mirroring the `buildSettingsDropdownItems` pattern above). The list is the
 * historical navigation exactly, with one gating change: the
 * `{ text: 'Builds', href: '/builds' }` entry is included only when
 * `canAccessBuilds(role)`, so roles without builds access no longer see a link
 * to a page that can only render a 403 banner. The PortalAdmin-only group
 * (including "Build Fleet" → `/admin/fleet`) and the UseCaseAdmin audit-logs
 * handling are unchanged.
 */
export function buildNavigationItems(
  role: UserRole | undefined
): SideNavigationProps.Item[] {
  // Base navigation items for all users
  const baseNavigationItems: SideNavigationProps.Item[] = [
    { type: 'link' as const, text: 'Dashboard', href: '/dashboard' },
    { type: 'link' as const, text: 'Use Cases', href: '/usecases' },
    { type: 'divider' as const },
    { type: 'link' as const, text: 'Data Management', href: '/data' },
    { type: 'link' as const, text: 'Labeling', href: '/labeling' },
    { type: 'link' as const, text: 'Training', href: '/training' },
    { type: 'link' as const, text: 'Models', href: '/models' },
    { type: 'divider' as const },
    { type: 'link' as const, text: 'Workflows', href: '/workflows/builder' },
    { type: 'link' as const, text: 'Node Designer', href: '/node-designer' },
    { type: 'link' as const, text: 'Components', href: '/components' },
    // The builds surface is limited to the roles holding `builds:*`
    // (DataScientist, UseCaseAdmin, PortalAdmin) — Req 2.5, 2.6.
    ...(canAccessBuilds(role)
      ? [{ type: 'link' as const, text: 'Builds', href: '/builds' }]
      : []),
    { type: 'link' as const, text: 'Deployments', href: '/deployments' },
    { type: 'link' as const, text: 'Devices', href: '/devices' },
  ];

  // Admin-only items (PortalAdmin only)
  const portalAdminItems: SideNavigationProps.Item[] = [
    { type: 'divider' as const },
    { type: 'link' as const, text: 'Plugin Review', href: '/node-designer/review' },
    // Build server fleet management is PortalAdmin-only, like the
    // UserManager entry (portal-build-fleet-and-workflow-gates Req 6.1, 6.7).
    { type: 'link' as const, text: 'Build Fleet', href: '/admin/fleet' },
    { type: 'link' as const, text: 'Settings', href: '/settings' },
  ];

  // Audit logs - available to PortalAdmin and UseCaseAdmin
  const auditLogsItem: SideNavigationProps.Item = {
    type: 'link' as const,
    text: 'Audit Logs',
    href: '/audit'
  };

  // Combine navigation items based on user role
  const isPortalAdmin = role === 'PortalAdmin';
  const isUseCaseAdmin = role === 'UseCaseAdmin';

  if (isPortalAdmin) {
    return [...baseNavigationItems, ...portalAdminItems, auditLogsItem];
  }
  if (isUseCaseAdmin) {
    return [...baseNavigationItems, { type: 'divider' as const }, auditLogsItem];
  }
  return [...baseNavigationItems];
}

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, changePassword } = useAuth();
  const config = getConfig();
  const branding = config.branding;
  const build = getBuildInfo();

  // The workflow builder needs the full horizontal space, so the side
  // navigation is collapsed by default on that route. The route only
  // provides the default: once the user toggles the navigation the
  // explicit choice wins for the rest of the session, whatever page
  // they navigate to.
  const isBuilderRoute = location.pathname.startsWith('/workflows/builder');
  const [navigationOverride, setNavigationOverride] = useState<boolean | null>(null);
  const navigationOpen = navigationOverride ?? !isBuilderRoute;

  const [showChangePassword, setShowChangePassword] = useState(false);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [changePwLoading, setChangePwLoading] = useState(false);
  const [changePwError, setChangePwError] = useState('');
  const [changePwSuccess, setChangePwSuccess] = useState(false);

  const navigationItems = buildNavigationItems(user?.role);

  return (
    <>
      <TopNavigation
        identity={{
          href: '/',
          title: branding.applicationName,
          logo: branding.logoUrl ? {
            src: branding.logoUrl,
            alt: branding.companyName,
          } : undefined,
        }}
        utilities={[
          {
            type: 'menu-dropdown',
            text: user?.email || user?.username || 'User',
            description: user?.role || '',
            iconName: 'user-profile',
            items: buildSettingsDropdownItems(user?.role),
            onItemClick: async ({ detail }) => {
              if (detail.id === 'logout') {
                await logout();
                navigate('/login');
              } else if (detail.id === 'settings') {
                navigate('/settings');
              } else if (detail.id === 'user-manager') {
                navigate('/admin/user-manager');
              } else if (detail.id === 'change-password') {
                setShowChangePassword(true);
                setChangePwError('');
                setChangePwSuccess(false);
                setOldPassword('');
                setNewPassword('');
                setConfirmPassword('');
              }
            },
          },
        ]}
      />
      <AppLayout
        // The workflow builder canvas needs the full content width: drop
        // the AppLayout content gutters and its default max content width
        // (the dead space between the side navigation and the node
        // palette) on the builder route only; the builder page applies
        // its own minimal internal padding. Every other page keeps the
        // default Cloudscape content paddings and width.
        disableContentPaddings={isBuilderRoute}
        maxContentWidth={isBuilderRoute ? Number.MAX_VALUE : undefined}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOverride(detail.open)}
        navigation={
          <SideNavigation
            activeHref={location.pathname}
            items={navigationItems}
            onFollow={(event) => {
              event.preventDefault();
              navigate(event.detail.href);
            }}
          />
        }
        content={<Outlet />}
        toolsHide
        navigationWidth={200}
      />

      {/* Bottom version banner — shows the exact portal build that is running.
          Fixed to the viewport bottom so it persists across all pages. */}
      <div
        style={{
          position: 'fixed',
          bottom: 0,
          left: 0,
          right: 0,
          zIndex: 1000,
          background: '#232f3e',
          color: '#d1d5db',
          fontSize: '12px',
          lineHeight: '24px',
          height: '24px',
          padding: '0 12px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          fontFamily: 'monospace',
          borderTop: '1px solid #414d5c',
        }}
        title={`Build time: ${build.buildTime || 'unknown'}`}
      >
        <span>{branding.applicationName}</span>
        <span>
          {`v${build.version}`}
          {build.gitSha && build.gitSha !== 'unknown' ? ` · ${build.gitSha}` : ''}
          {build.buildTime ? ` · built ${build.buildTime.slice(0, 10)}` : ''}
        </span>
      </div>

      <Modal
        visible={showChangePassword}
        onDismiss={() => setShowChangePassword(false)}
        header="Change Password"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setShowChangePassword(false)}>Cancel</Button>
              <Button
                variant="primary"
                loading={changePwLoading}
                onClick={async () => {
                  setChangePwError('');
                  if (!oldPassword || !newPassword || !confirmPassword) {
                    setChangePwError('All fields are required');
                    return;
                  }
                  if (newPassword !== confirmPassword) {
                    setChangePwError('New passwords do not match');
                    return;
                  }
                  if (newPassword.length < 8) {
                    setChangePwError('Password must be at least 8 characters');
                    return;
                  }
                  try {
                    setChangePwLoading(true);
                    await changePassword(oldPassword, newPassword);
                    setChangePwSuccess(true);
                  } catch (err: any) {
                    setChangePwError(err.message || 'Failed to change password');
                  } finally {
                    setChangePwLoading(false);
                  }
                }}
              >
                Change Password
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          {changePwError && <Alert type="error">{changePwError}</Alert>}
          {changePwSuccess && (
            <Alert type="success">Password changed successfully.</Alert>
          )}
          {!changePwSuccess && (
            <>
              <FormField label="Current Password">
                <Input
                  type="password"
                  value={oldPassword}
                  onChange={({ detail }) => setOldPassword(detail.value)}
                />
              </FormField>
              <FormField label="New Password">
                <Input
                  type="password"
                  value={newPassword}
                  onChange={({ detail }) => setNewPassword(detail.value)}
                />
              </FormField>
              <FormField label="Confirm New Password">
                <Input
                  type="password"
                  value={confirmPassword}
                  onChange={({ detail }) => setConfirmPassword(detail.value)}
                />
              </FormField>
            </>
          )}
        </SpaceBetween>
      </Modal>
    </>
  );
}
