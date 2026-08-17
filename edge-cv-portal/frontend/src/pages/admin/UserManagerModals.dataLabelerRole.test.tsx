/**
 * Bug condition exploration tests for the user-manager-datalabeler-role
 * bugfix (defects 1.1 and 1.2): the User Manager modals must offer the
 * backend's SIX-role vocabulary — the frontend PORTAL_ROLES has diverged
 * (five roles) from the backend `user_admin.py` PORTAL_ROLES six-tuple
 * (DataLabeler appended by dda-data-labeling, Requirement 2.1).
 *
 * Property 1: Bug Condition — DataLabeler Offered.
 * **Validates: Requirements 1.1, 1.2 (bug condition) / 2.1, 2.2 (expected
 * behavior once fixed)**
 *
 * EXPECTED TO FAIL on the unfixed tree (both dropdowns offer only the five
 * original roles). The same assertions validate the fix once
 * 'DataLabeler' is appended to the frontend PORTAL_ROLES (task 3.3).
 *
 * Conventions follow UserManagerModals.test.tsx: Cloudscape createWrapper
 * test-utils, hoisted apiService mock.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { CreateUserModal, RoleModal } from './UserManagerModals';
import type { AdminAccount } from '../../services/api';

const { createAdminUser, setAdminUserRole } = vi.hoisted(() => ({
  createAdminUser: vi.fn(),
  setAdminUserRole: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    createAdminUser,
    setAdminUserRole,
  },
}));

/**
 * The backend's six-role vocabulary — the tuple in
 * edge-cv-portal/backend/functions/user_admin.py PORTAL_ROLES
 * ('PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer',
 * 'DataLabeler'). Deliberately spelled out here (NOT imported from
 * UserManagerModals) so the test pins the expected vocabulary
 * independently of the frontend array under test.
 */
const BACKEND_PORTAL_ROLES = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
  'DataLabeler',
];

/**
 * The five ORIGINAL Portal_Role values (portal-user-manager Requirement
 * 5.2) whose presence and order must be preserved (this spec's
 * Requirement 3.1). Spelled out independently of the frontend array under
 * test, like BACKEND_PORTAL_ROLES above.
 */
const ORIGINAL_FIVE_ROLES = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

const ACCOUNT: AdminAccount = {
  username: 'operator1',
  email: 'op1@example.com',
  email_verified: true,
  role: 'Operator',
  user_status: 'CONFIRMED',
  enabled: true,
  edge_capable: false,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Property 1: bug condition exploration — DataLabeler offered (defects 1.1, 1.2)', () => {
  it('CreateUserModal role dropdown offers exactly the six-role backend vocabulary (defect 1.1)', () => {
    render(<CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />);

    const select = createWrapper(document.body).findSelect()!;
    select.openDropdown();
    const options = select.findDropdown().findOptions();

    expect(options.map((o) => o.getElement().textContent)).toEqual(
      BACKEND_PORTAL_ROLES
    );
  });

  it('RoleModal role dropdown offers exactly the six-role backend vocabulary with the current role preselected (defect 1.2)', () => {
    render(
      <RoleModal account={ACCOUNT} onSuccess={vi.fn()} onDismiss={vi.fn()} />
    );

    const select = createWrapper(document.body).findSelect()!;

    // Current role preselected in the trigger (preserved behavior).
    expect(select.findTrigger().getElement()).toHaveTextContent('Operator');

    select.openDropdown();
    const options = select.findDropdown().findOptions();

    expect(options.map((o) => o.getElement().textContent)).toEqual(
      BACKEND_PORTAL_ROLES
    );
  });
});

/**
 * Property 2: Preservation — the five original roles keep their existing
 * order (task 2, observation-first). These cases pass on the UNFIXED
 * five-role tree (the whole option list IS the five) and must keep
 * passing after 'DataLabeler' is appended LAST (the five remain an
 * in-order PREFIX of the option list).
 *
 * **Validates: Requirements 3.1**
 */
describe('Property 2: preservation — five original roles in order as a prefix (Requirement 3.1)', () => {
  it('CreateUserModal role dropdown offers the five original roles in order as a prefix of its options', () => {
    render(<CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />);

    const select = createWrapper(document.body).findSelect()!;
    select.openDropdown();
    const options = select
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent);

    expect(options.slice(0, ORIGINAL_FIVE_ROLES.length)).toEqual(
      ORIGINAL_FIVE_ROLES
    );
  });

  it('RoleModal role dropdown offers the five original roles in order as a prefix with the current role preselected', () => {
    render(
      <RoleModal account={ACCOUNT} onSuccess={vi.fn()} onDismiss={vi.fn()} />
    );

    const select = createWrapper(document.body).findSelect()!;

    // Current-role preselection preserved (Requirement 3.1).
    expect(select.findTrigger().getElement()).toHaveTextContent('Operator');

    select.openDropdown();
    const options = select
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent);

    expect(options.slice(0, ORIGINAL_FIVE_ROLES.length)).toEqual(
      ORIGINAL_FIVE_ROLES
    );
  });
});

/**
 * Property 3: Fix Checking — Frontend/Backend Role Vocabulary Parity
 * (task 4). Runs on the FIXED tree: every backend role is offered by BOTH
 * dropdowns, the option lists exactly equal the backend tuple in order,
 * and a DataLabeler selection submits through the existing handlers
 * unchanged in shape (`createAdminUser` payload role='DataLabeler';
 * `setAdminUserRole(username, 'DataLabeler')` — the same payload shapes
 * UserManagerModals.test.tsx pins for other roles).
 *
 * **Validates: Requirements 2.1, 2.2, 2.3, 3.1**
 */
describe('Property 3: fix check — frontend/backend role vocabulary parity (Requirements 2.1, 2.2, 2.3, 3.1)', () => {
  /** Open the (single) role select on the page and read its option texts. */
  function openRoleOptions(): (string | null)[] {
    const select = createWrapper(document.body).findSelect()!;
    select.openDropdown();
    return select
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent);
  }

  it.each(BACKEND_PORTAL_ROLES)(
    'role %s is offered by BOTH the CreateUserModal and RoleModal dropdowns',
    (role) => {
      const create = render(
        <CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />
      );
      expect(openRoleOptions()).toContain(role);
      create.unmount();

      render(
        <RoleModal
          account={ACCOUNT}
          onSuccess={vi.fn()}
          onDismiss={vi.fn()}
        />
      );
      expect(openRoleOptions()).toContain(role);
    }
  );

  it('both dropdowns\' option lists exactly equal the backend tuple, in order', () => {
    const create = render(
      <CreateUserModal onSuccess={vi.fn()} onDismiss={vi.fn()} />
    );
    expect(openRoleOptions()).toEqual(BACKEND_PORTAL_ROLES);
    create.unmount();

    render(
      <RoleModal account={ACCOUNT} onSuccess={vi.fn()} onDismiss={vi.fn()} />
    );
    expect(openRoleOptions()).toEqual(BACKEND_PORTAL_ROLES);
  });

  it('a DataLabeler create submission reaches createAdminUser with role DataLabeler (Requirement 2.3)', async () => {
    createAdminUser.mockResolvedValue({ message: 'ok' });
    const onSuccess = vi.fn();
    render(<CreateUserModal onSuccess={onSuccess} onDismiss={vi.fn()} />);

    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'labeler1' },
    });
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'labeler1@example.com' },
    });

    const select = createWrapper(document.body).findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('DataLabeler');

    fireEvent.click(screen.getByRole('button', { name: 'Create user' }));

    // The same payload shape the existing suites pin for other roles:
    // { username, email, role } — role carries the new value verbatim.
    await waitFor(() =>
      expect(createAdminUser).toHaveBeenCalledWith({
        username: 'labeler1',
        email: 'labeler1@example.com',
        role: 'DataLabeler',
      })
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('labeler1')
    );
  });

  it('a DataLabeler role change reaches setAdminUserRole(username, DataLabeler) (Requirements 2.2, 2.3)', async () => {
    setAdminUserRole.mockResolvedValue({ message: 'ok' });
    const onSuccess = vi.fn();
    render(
      <RoleModal account={ACCOUNT} onSuccess={onSuccess} onDismiss={vi.fn()} />
    );

    const select = createWrapper(document.body).findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('DataLabeler');

    fireEvent.click(screen.getByRole('button', { name: 'Change role' }));

    // Same (username, role) shape UserManagerModals.test.tsx pins for
    // the DataScientist change.
    await waitFor(() =>
      expect(setAdminUserRole).toHaveBeenCalledWith('operator1', 'DataLabeler')
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('DataLabeler')
    );
    expect(onSuccess).toHaveBeenCalledWith(
      expect.stringContaining('operator1')
    );
  });
});
