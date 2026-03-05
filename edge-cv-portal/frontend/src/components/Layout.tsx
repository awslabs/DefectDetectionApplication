import { useState } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import {
  AppLayout,
  TopNavigation,
  SideNavigation,
  SideNavigationProps,
  Modal,
  SpaceBetween,
  FormField,
  Input,
  Button,
  Box,
  Alert,
} from '@cloudscape-design/components';
import { useAuth } from '../contexts/AuthContext';
import { getConfig } from '../config';

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, changePassword } = useAuth();
  const config = getConfig();
  const branding = config.branding;
  const [showChangePassword, setShowChangePassword] = useState(false);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [changePwLoading, setChangePwLoading] = useState(false);
  const [changePwError, setChangePwError] = useState('');
  const [changePwSuccess, setChangePwSuccess] = useState(false);

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
    { type: 'link' as const, text: 'Components', href: '/components' },
    { type: 'link' as const, text: 'Deployments', href: '/deployments' },
    { type: 'link' as const, text: 'Devices', href: '/devices' },
  ];

  // Admin-only items (PortalAdmin only)
  const portalAdminItems: SideNavigationProps.Item[] = [
    { type: 'divider' as const },
    { type: 'link' as const, text: 'Settings', href: '/settings' },
  ];

  // Audit logs - available to PortalAdmin and UseCaseAdmin
  const auditLogsItem: SideNavigationProps.Item = { 
    type: 'link' as const, 
    text: 'Audit Logs', 
    href: '/audit' 
  };

  // Combine navigation items based on user role
  const isPortalAdmin = user?.role === 'PortalAdmin';
  const isUseCaseAdmin = user?.role === 'UseCaseAdmin';
  
  let navigationItems = [...baseNavigationItems];
  
  if (isPortalAdmin) {
    navigationItems = [...navigationItems, ...portalAdminItems, auditLogsItem];
  } else if (isUseCaseAdmin) {
    navigationItems = [...navigationItems, { type: 'divider' as const }, auditLogsItem];
  }

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
            items: [
              {
                id: 'profile',
                text: 'Profile',
                disabled: true,
              },
              {
                id: 'settings',
                text: 'Settings',
                disabled: true,
              },
              {
                id: 'change-password',
                text: 'Change Password',
              },
              {
                id: 'logout',
                text: 'Sign out',
              },
            ],
            onItemClick: async ({ detail }) => {
              if (detail.id === 'logout') {
                await logout();
                navigate('/login');
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
