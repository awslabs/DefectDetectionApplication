/**
 * Use Cases page: PortalAdmin-only actions and Registration_Report remedies
 * (spec: portal-authorization-and-iam-scoping, task 8.1; design I1 D1, I3
 * D3, I5 "The Attach_Step").
 *
 * - "Onboard New Use Case" and the "Re-provision Shared Components" row
 *   action are shown to a PortalAdmin (server-resolved `is_super_user`) and
 *   hidden from a UseCaseAdmin and a Viewer (cosmetic; the server decides);
 * - an "Allow" answer whose `registration[bucket].remedy` is set shows the
 *   remedy beside the bucket in the Edit modal; no remedy, nothing shown;
 * - a saved edit whose `registration` names a remedy shows it on the page
 *   after the modal closes.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import UseCases, { rowActionsFor } from './UseCases';
import { UseCase } from '../types';

const {
  listUseCases,
  updateUseCase,
  deleteUseCase,
  provisionSharedComponents,
  getSharedComponentsStatus,
  allowBucketForUsecase,
  removeAllowedBucket,
  getMyUsecases,
  useAuthMock,
} = vi.hoisted(() => ({
  listUseCases: vi.fn(),
  updateUseCase: vi.fn(),
  deleteUseCase: vi.fn(),
  provisionSharedComponents: vi.fn(),
  getSharedComponentsStatus: vi.fn(),
  allowBucketForUsecase: vi.fn(),
  removeAllowedBucket: vi.fn(),
  getMyUsecases: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: {
      listUseCases,
      updateUseCase,
      deleteUseCase,
      provisionSharedComponents,
      getSharedComponentsStatus,
      allowBucketForUsecase,
      removeAllowedBucket,
      getMyUsecases,
    },
  };
});

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../contexts/AuthContext')>();
  return { ...actual, useAuth: useAuthMock };
});

const REMEDY =
  'Enable ABAC on the bucket: aws s3api put-bucket-abac --bucket extra-bucket --abac-status Status=Enabled';

function usecase(overrides: Partial<UseCase> = {}): UseCase {
  return {
    usecase_id: 'uc-1',
    name: 'Line 3',
    account_id: '111111111111',
    s3_bucket: 'line3-bucket',
    cross_account_role_arn: '',
    sagemaker_execution_role_arn: '',
    external_id: '',
    owner: 'owner@example.com',
    created_at: 1730000000000,
    updated_at: 1730000000000,
    ...overrides,
  } as UseCase;
}

/** The caller's server-resolved role: PortalAdmin is `is_super_user`. */
function grantRole(role: 'PortalAdmin' | 'UseCaseAdmin' | 'Viewer') {
  getMyUsecases.mockResolvedValue({
    usecases: role === 'PortalAdmin' ? [] : [{ usecase_id: 'uc-1', name: 'Line 3', user_role: role }],
    total_count: role === 'PortalAdmin' ? 0 : 1,
    is_super_user: role === 'PortalAdmin',
  });
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role } });
}

function renderPage(item: UseCase = usecase()) {
  listUseCases.mockResolvedValue({ usecases: [item] });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UseCases />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function settle() {
  await waitFor(() => expect(getMyUsecases).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function openRowActions() {
  await screen.findByText('Line 3');
  const buttons = screen.getAllByRole('button', { name: /Actions/i });
  fireEvent.click(buttons[buttons.length - 1]);
  await screen.findByText('Edit');
}

async function openEditModal() {
  await openRowActions();
  fireEvent.click(screen.getByText('Edit'));
  const header = await screen.findByText('Edit Use Case');
  return within(header.closest('[role="dialog"]') as HTMLElement);
}

beforeEach(() => {
  vi.clearAllMocks();
  getSharedComponentsStatus.mockResolvedValue({ usecases: [], usecases_needing_update: 0 });
  updateUseCase.mockResolvedValue({ message: 'Use case updated successfully' });
});

describe('rowActionsFor', () => {
  it('offers re-provisioning to a PortalAdmin only', () => {
    expect(rowActionsFor(true).map((a) => a.id)).toEqual(['browse', 'team', 'provision', 'edit', 'delete']);
    expect(rowActionsFor(false).map((a) => a.id)).toEqual(['browse', 'team', 'edit', 'delete']);
  });
});

describe('PortalAdmin-only actions', () => {
  it('shows both actions to a PortalAdmin', async () => {
    grantRole('PortalAdmin');
    renderPage();
    await settle();
    expect(await screen.findByRole('button', { name: 'Onboard New Use Case' })).toBeInTheDocument();
    await openRowActions();
    expect(screen.getByText('Re-provision Shared Components')).toBeInTheDocument();
  });

  it.each(['UseCaseAdmin', 'Viewer'] as const)('hides both actions from a %s', async (role) => {
    grantRole(role);
    renderPage();
    await settle();
    await openRowActions();
    expect(screen.queryByRole('button', { name: 'Onboard New Use Case' })).not.toBeInTheDocument();
    expect(screen.queryByText('Re-provision Shared Components')).not.toBeInTheDocument();
    // The other row actions stay.
    expect(screen.getByText('Manage Team')).toBeInTheDocument();
  });
});

describe('Registration_Report remedies', () => {
  function putResponse(registration: Record<string, unknown>) {
    return {
      message: 'Use case updated successfully',
      added: ['extra-bucket'],
      already_present: [],
      removed: [],
      not_present: [],
      allowed_s3_buckets: ['line3-bucket', 'extra-bucket'],
      registration,
    };
  }

  async function allow(dialog: ReturnType<typeof within>, name: string) {
    fireEvent.change(dialog.getByPlaceholderText('another-bucket'), { target: { value: name } });
    const button = dialog.getByRole('button', { name: 'Allow' });
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
  }

  it('shows the remedy of an allowed bucket beside it in the Edit modal', async () => {
    grantRole('UseCaseAdmin');
    allowBucketForUsecase.mockResolvedValue(
      putResponse({
        'extra-bucket': { tagged: true, abac_enabled: false, iam_reachable: false, remedy: REMEDY },
      })
    );
    renderPage();
    const dialog = await openEditModal();
    await allow(dialog, 'extra-bucket');
    await waitFor(() => expect(dialog.getByText(REMEDY, { exact: false })).toBeInTheDocument());
    expect(dialog.getByText('Needs one more step')).toBeInTheDocument();
  });

  it('shows nothing extra when the bucket is reachable', async () => {
    grantRole('UseCaseAdmin');
    allowBucketForUsecase.mockResolvedValue(
      putResponse({
        'extra-bucket': { tagged: true, abac_enabled: true, iam_reachable: true, remedy: null },
      })
    );
    renderPage();
    const dialog = await openEditModal();
    await allow(dialog, 'extra-bucket');
    await waitFor(() => expect(dialog.getByText('extra-bucket')).toBeInTheDocument());
    expect(dialog.queryByText('Needs one more step')).not.toBeInTheDocument();
  });

  it('shows the remedy of a saved edit after the modal closes', async () => {
    grantRole('UseCaseAdmin');
    updateUseCase.mockResolvedValue({
      message: 'Use case updated successfully',
      registration: {
        'line3-bucket': { tagged: true, abac_enabled: false, iam_reachable: false, remedy: REMEDY },
      },
    });
    renderPage();
    const dialog = await openEditModal();
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(updateUseCase).toHaveBeenCalled());
    expect(
      await screen.findByText('The use case was saved; a bucket needs one more step')
    ).toBeInTheDocument();
    expect(screen.getByText(REMEDY, { exact: false })).toBeInTheDocument();
  });

  it('shows no remedy notice after a save whose registration is empty', async () => {
    grantRole('UseCaseAdmin');
    updateUseCase.mockResolvedValue({ message: 'Use case updated successfully', registration: {} });
    renderPage();
    const dialog = await openEditModal();
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));
    await screen.findByText('Use case updated successfully');
    expect(
      screen.queryByText('The use case was saved; a bucket needs one more step')
    ).not.toBeInTheDocument();
  });
});
