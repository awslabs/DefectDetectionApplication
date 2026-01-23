import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import {
  AppLayout,
  TopNavigation,
  SideNavigation,
  SideNavigationProps,
} from '@cloudscape-design/components';
import { useAuth } from '../contexts/AuthContext';
import { getConfig } from '../config';

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuth();
  const config = getConfig();
  const branding = config.branding;

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
                id: 'logout',
                text: 'Sign out',
              },
            ],
            onItemClick: async ({ detail }) => {
              if (detail.id === 'logout') {
                await logout();
                navigate('/login');
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
    </>
  );
}
