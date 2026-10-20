/**
 * Onboarding wizard: use-case account refusals by `error_code` and the
 * Registration_Report remedy of a created use case (spec: portal-
 * authorization-and-iam-scoping, task 8.1; design I1 "Error handling",
 * I5 "The Attach_Step").
 *
 * - `ACCOUNT_NOT_TRUSTED` and `ACCOUNT_CONTROL_NOT_VERIFIED` from
 *   `POST /usecases/verify-role` and `POST /usecases` render a notice by
 *   code with the server's `error` and, when present, its `remedy`;
 * - every other error keeps the generic message path (no account notice);
 * - a 201 whose `registration` names a remedy shows it on the review step.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import UseCaseOnboarding from './UseCaseOnboarding';
import AccountErrorNotice, {
  ACCOUNT_ERROR_HEADINGS,
  accountErrorCode,
} from '../components/AccountErrorNotice';
import { ApiError } from '../services/api';

const {
  listDataAccounts,
  listS3Buckets,
  createS3Bucket,
  getMyUsecases,
  verifyRole,
  createUseCase,
  useAuthMock,
} = vi.hoisted(() => ({
  listDataAccounts: vi.fn(),
  listS3Buckets: vi.fn(),
  createS3Bucket: vi.fn(),
  getMyUsecases: vi.fn(),
  verifyRole: vi.fn(),
  createUseCase: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: {
      listDataAccounts,
      listS3Buckets,
      createS3Bucket,
      getMyUsecases,
      verifyRole,
      createUseCase,
    },
  };
});

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../contexts/AuthContext')>();
  return { ...actual, useAuth: useAuthMock };
});

const UNTRUSTED = '4'.repeat(12);
const NOT_TRUSTED_REMEDY = 'Add the account to trustedUseCaseAccountIds and redeploy the portal.';
const CONTROL_REMEDY =
  'Deploy DDAPortalUseCaseAccountStack in the use-case account and supply its external id.';

function apiError(status: number, body: Record<string, unknown>): ApiError {
  return new ApiError(String(body.error || `HTTP ${status}`), status, undefined, body);
}

function renderWizard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UseCaseOnboarding />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function next() {
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await settle();
}

/** Multi-account: Setup Type → Basic Information → Configure S3 Storage (role details shown). */
async function toRoleStep() {
  renderWizard();
  await settle();
  await next();
  fireEvent.change(screen.getByPlaceholderText('e.g., Manufacturing Line 1 Defect Detection'), {
    target: { value: 'Line 3' },
  });
  await next();
  fireEvent.click(await screen.findByRole('button', { name: 'Mark as Deployed' }));
  fireEvent.change(await screen.findByLabelText('AWS Account ID'), { target: { value: UNTRUSTED } });
  fireEvent.change(screen.getByLabelText('Role ARN'), {
    target: { value: `arn:aws:iam::${UNTRUSTED}:role/DDAPortalAccessRole` },
  });
  fireEvent.change(screen.getByLabelText('External ID'), { target: { value: 'external-id' } });
  for (const input of screen.getAllByPlaceholderText(/my-.*-bucket/)) {
    fireEvent.change(input, { target: { value: 'line3-bucket' } });
  }
}

async function submitFromRoleStep() {
  await next();
  await screen.findByText('Configuration Summary');
  fireEvent.click(screen.getByRole('button', { name: 'Create Use Case' }));
  await waitFor(() => expect(createUseCase).toHaveBeenCalled());
  await settle();
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'PortalAdmin' } });
  listDataAccounts.mockResolvedValue({ data_accounts: [] });
  listS3Buckets.mockResolvedValue({ buckets: [], count: 0 });
  getMyUsecases.mockResolvedValue({ usecases: [], total_count: 0, is_super_user: true });
});

describe('accountErrorCode and the notice', () => {
  it('recognizes exactly the two account codes', () => {
    expect(accountErrorCode(apiError(400, { error: 'x', error_code: 'ACCOUNT_NOT_TRUSTED' }))).toBe(
      'ACCOUNT_NOT_TRUSTED'
    );
    expect(
      accountErrorCode(apiError(400, { error: 'x', error_code: 'ACCOUNT_CONTROL_NOT_VERIFIED' }))
    ).toBe('ACCOUNT_CONTROL_NOT_VERIFIED');
    expect(accountErrorCode(apiError(400, { error: 'x', error_code: 'INVALID_ROLE_ARN' }))).toBeUndefined();
    expect(accountErrorCode(apiError(503, { error: 'x', error_code: 'STS_UNAVAILABLE' }))).toBeUndefined();
    expect(accountErrorCode(new Error('plain'))).toBeUndefined();
  });

  it('renders nothing for an error without an account code', () => {
    const { container } = render(
      <AccountErrorNotice err={apiError(400, { error: 'other', error_code: 'bucket_not_eligible' })} />
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe('verify-role refusals', () => {
  it('renders ACCOUNT_NOT_TRUSTED by code with the server error', async () => {
    verifyRole.mockRejectedValue(
      apiError(400, {
        status: 'failed',
        error: `Account ${UNTRUSTED} is not a trusted use-case account`,
        error_code: 'ACCOUNT_NOT_TRUSTED',
      })
    );
    await toRoleStep();
    fireEvent.click(screen.getByRole('button', { name: 'Verify Role' }));
    expect(await screen.findByText(ACCOUNT_ERROR_HEADINGS.ACCOUNT_NOT_TRUSTED)).toBeInTheDocument();
    expect(screen.getByText(`Account ${UNTRUSTED} is not a trusted use-case account`)).toBeInTheDocument();
    expect(screen.queryByText('Role verified successfully')).not.toBeInTheDocument();
  });
});

describe('create refusals', () => {
  it('renders ACCOUNT_CONTROL_NOT_VERIFIED with its remedy', async () => {
    createUseCase.mockRejectedValue(
      apiError(400, {
        error: 'The portal could not assume the role with the supplied external id.',
        error_code: 'ACCOUNT_CONTROL_NOT_VERIFIED',
        remedy: CONTROL_REMEDY,
      })
    );
    await toRoleStep();
    await submitFromRoleStep();
    expect(
      await screen.findByText(ACCOUNT_ERROR_HEADINGS.ACCOUNT_CONTROL_NOT_VERIFIED)
    ).toBeInTheDocument();
    expect(
      screen.getByText('The portal could not assume the role with the supplied external id.')
    ).toBeInTheDocument();
    expect(screen.getByText(CONTROL_REMEDY)).toBeInTheDocument();
  });

  it('renders ACCOUNT_NOT_TRUSTED with its remedy', async () => {
    createUseCase.mockRejectedValue(
      apiError(400, {
        error: `Account ${UNTRUSTED} is not a trusted use-case account`,
        error_code: 'ACCOUNT_NOT_TRUSTED',
        remedy: NOT_TRUSTED_REMEDY,
      })
    );
    await toRoleStep();
    await submitFromRoleStep();
    expect(await screen.findByText(ACCOUNT_ERROR_HEADINGS.ACCOUNT_NOT_TRUSTED)).toBeInTheDocument();
    expect(screen.getByText(NOT_TRUSTED_REMEDY)).toBeInTheDocument();
  });

  it('keeps the generic path for any other error', async () => {
    createUseCase.mockRejectedValue(
      apiError(503, { error: 'STS is temporarily unavailable; retry', error_code: 'STS_UNAVAILABLE' })
    );
    await toRoleStep();
    await submitFromRoleStep();
    expect(screen.queryByText(ACCOUNT_ERROR_HEADINGS.ACCOUNT_NOT_TRUSTED)).not.toBeInTheDocument();
    expect(
      screen.queryByText(ACCOUNT_ERROR_HEADINGS.ACCOUNT_CONTROL_NOT_VERIFIED)
    ).not.toBeInTheDocument();
  });
});

describe('the created use case remedy', () => {
  it('shows the Registration_Report remedy on the review step', async () => {
    const remedy = 'Enable ABAC on the bucket: aws s3api put-bucket-abac --bucket line3-bucket --abac-status Status=Enabled';
    createUseCase.mockResolvedValue({
      usecase: { usecase_id: 'uc-1', name: 'Line 3' },
      message: 'created',
      registration: {
        'line3-bucket': { tagged: true, abac_enabled: false, iam_reachable: false, remedy },
      },
    });
    await toRoleStep();
    await submitFromRoleStep();
    expect(await screen.findByText('Use case created; a bucket needs one more step')).toBeInTheDocument();
    expect(screen.getByText(remedy, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Go to Use Cases' })).toBeInTheDocument();
  });

  it('shows no remedy notice when every attached bucket is reachable', async () => {
    createUseCase.mockResolvedValue({
      usecase: { usecase_id: 'uc-1', name: 'Line 3' },
      message: 'created',
      registration: {
        'line3-bucket': { tagged: true, abac_enabled: true, iam_reachable: true, remedy: null },
      },
    });
    await toRoleStep();
    await submitFromRoleStep();
    expect(screen.queryByText('Use case created; a bucket needs one more step')).not.toBeInTheDocument();
  });
});
