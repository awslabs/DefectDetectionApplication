/**
 * Data Management: Registration_Report remedies beside a bucket (spec:
 * portal-authorization-and-iam-scoping, task 8.1; design I5).
 *
 * - a bucket list entry the portal's role could not read carries `remedy`
 *   (abac_enabled false), shown beside the bucket;
 * - an "Allow for this use case" answer whose `registration[bucket].remedy`
 *   is set shows that remedy beside the bucket after the refresh;
 * - an answer without a remedy shows none.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import DataManagement from './DataManagement';
import { ALLOW_ACTION_LABEL } from '../components/BucketNotAllowedNotice';

const {
  listUseCases,
  getUseCase,
  listDataBuckets,
  listDataFolders,
  allowBucketForUsecase,
  getMyUsecases,
  useAuthMock,
} = vi.hoisted(() => ({
  listUseCases: vi.fn(),
  getUseCase: vi.fn(),
  listDataBuckets: vi.fn(),
  listDataFolders: vi.fn(),
  allowBucketForUsecase: vi.fn(),
  getMyUsecases: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: {
      listUseCases,
      getUseCase,
      listDataBuckets,
      listDataFolders,
      allowBucketForUsecase,
      getMyUsecases,
    },
  };
});

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../contexts/AuthContext')>();
  return { ...actual, useAuth: useAuthMock };
});

const USECASE_ID = 'uc-1';
const CONFIGURED = 'line3-bucket';
const FOREIGN = 'other-team-bucket';
const LIST_REMEDY = 'Enable ABAC on the bucket so the portal can read it';
const ALLOW_REMEDY = 'Redeploy the portal so its roles reach this bucket';

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DataManagement />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const cards = () => within(screen.getByTestId('data-bucket-cards'));

function putResponse(remedy: string | null) {
  return {
    message: 'ok',
    added: [FOREIGN],
    already_present: [],
    removed: [],
    not_present: [],
    allowed_s3_buckets: [FOREIGN],
    registration: {
      [FOREIGN]: { tagged: true, abac_enabled: remedy === null, iam_reachable: remedy === null, remedy },
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'UseCaseAdmin' } });
  listUseCases.mockResolvedValue({ usecases: [{ usecase_id: USECASE_ID, name: 'Line 3' }], count: 1 });
  getUseCase.mockResolvedValue({
    usecase: {
      usecase_id: USECASE_ID,
      name: 'Line 3',
      s3_bucket: CONFIGURED,
      allowed_s3_buckets: [],
      additional_s3_buckets: [],
    },
  });
  listDataFolders.mockResolvedValue({ bucket: CONFIGURED, prefix: '', folders: [], files: [], is_truncated: false });
  getMyUsecases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'Line 3', user_role: 'UseCaseAdmin' }],
    total_count: 1,
    is_super_user: false,
  });
});

describe('bucket remedies', () => {
  it('shows the remedy a bucket list entry carries', async () => {
    listDataBuckets.mockResolvedValue({
      buckets: [
        { name: FOREIGN, region: 'unknown', allowed: false, abac_enabled: false, remedy: LIST_REMEDY },
      ],
      current_data_bucket: null,
      target_account: 'portal-account',
      has_data_account_role: false,
    });
    renderPage();
    await screen.findByText('Select a Data Bucket');
    await waitFor(() => expect(cards().getByText(LIST_REMEDY, { exact: false })).toBeInTheDocument());
  });

  it('shows the remedy of an Allow answer beside the bucket', async () => {
    listDataBuckets.mockResolvedValue({
      buckets: [{ name: FOREIGN, region: 'us-east-1', allowed: false }],
      current_data_bucket: null,
      target_account: 'portal-account',
      has_data_account_role: false,
    });
    allowBucketForUsecase.mockResolvedValue(putResponse(ALLOW_REMEDY));
    renderPage();
    await screen.findByText('Select a Data Bucket');
    const allow = await waitFor(() => cards().getByRole('button', { name: ALLOW_ACTION_LABEL }));
    fireEvent.click(allow);
    await waitFor(() => expect(allowBucketForUsecase).toHaveBeenCalledWith(USECASE_ID, FOREIGN));
    await waitFor(() => expect(cards().getByText(ALLOW_REMEDY, { exact: false })).toBeInTheDocument());
  });

  it('shows no remedy when the Allow answer reports the bucket reachable', async () => {
    listDataBuckets.mockResolvedValue({
      buckets: [{ name: FOREIGN, region: 'us-east-1', allowed: false }],
      current_data_bucket: null,
      target_account: 'portal-account',
      has_data_account_role: false,
    });
    allowBucketForUsecase.mockResolvedValue(putResponse(null));
    renderPage();
    await screen.findByText('Select a Data Bucket');
    const allow = await waitFor(() => cards().getByRole('button', { name: ALLOW_ACTION_LABEL }));
    fireEvent.click(allow);
    await waitFor(() => expect(allowBucketForUsecase).toHaveBeenCalled());
    await waitFor(() => expect(getUseCase.mock.calls.length).toBeGreaterThan(1));
    expect(cards().queryByText('Needs one more step')).not.toBeInTheDocument();
  });
});
