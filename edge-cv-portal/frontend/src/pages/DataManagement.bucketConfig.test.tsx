/**
 * Data Management bucket marking and the inline allow action (spec:
 * portal-usecase-bucket-config, design §10.2, tasks 7.2 / 7.5).
 *
 * Through the rendered page with `apiService` mocked (`/users/me/usecases`,
 * `/usecases/{id}`, `/data/buckets`, `/data/folders`):
 * - each bucket is marked Allowed / Not allowed / Portal-created, and the
 *   Allowed badge's tooltip names the labeling-job exception;
 * - the inline "Allow for this use case" action is visible to a UseCaseAdmin,
 *   hidden from a Viewer, and replaced by the Data Account note when the
 *   buckets live in a separate Data Account;
 * - a refused allow (400 `bucket_not_eligible`) renders the notice, a
 *   successful one refreshes the use case record and the bucket list;
 * - the default selection is the use case's data bucket, else the first
 *   allowed bucket, else none;
 * - a `bucket_not_allowed` folder listing renders the notice in place of
 *   the folder table.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import DataManagement, {
  ALLOWED_BADGE_TOOLTIP,
  DATA_ACCOUNT_CANNOT_ADD_MESSAGE,
  defaultBucketSelection,
} from './DataManagement';
import { ALLOW_ACTION_LABEL } from '../components/BucketNotAllowedNotice';
import { ApiError } from '../services/api';

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

type Bucket = {
  name: string;
  region: string;
  is_configured?: boolean;
  allowed?: boolean;
  portal_created?: boolean;
};

function bucketsResponse(buckets: Bucket[], currentDataBucket: string | null, hasDataRole = false) {
  return {
    buckets,
    current_data_bucket: currentDataBucket,
    target_account: 'portal-account',
    has_data_account_role: hasDataRole,
  };
}

function grantRole(role: string | null, superUser = false) {
  getMyUsecases.mockResolvedValue({
    usecases: role ? [{ usecase_id: USECASE_ID, name: 'Line 3', user_role: role }] : [],
    total_count: role ? 1 : 0,
    is_super_user: superUser,
  });
}

function bucketError(errorCode: string, extra: Record<string, unknown> = {}, status = 400): ApiError {
  const body = { error: `refused: ${errorCode}`, error_code: errorCode, ...extra };
  return new ApiError(body.error, status, undefined, body);
}

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

// The bucket selector modal renders its table even while hidden, so the
// selection-view assertions are scoped to the cards.
const cards = () => within(screen.getByTestId('data-bucket-cards'));
const actionButtons = () => cards().queryAllByRole('button', { name: ALLOW_ACTION_LABEL });

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'Viewer' } });
  listUseCases.mockResolvedValue({ usecases: [{ usecase_id: USECASE_ID, name: 'Line 3' }], count: 1 });
  getUseCase.mockResolvedValue({
    usecase: {
      usecase_id: USECASE_ID,
      name: 'Line 3',
      s3_bucket: CONFIGURED,
      allowed_s3_buckets: [CONFIGURED],
      additional_s3_buckets: [],
    },
  });
  listDataFolders.mockResolvedValue({ bucket: CONFIGURED, prefix: '', folders: [], files: [], is_truncated: false });
  grantRole('UseCaseAdmin');
});

describe('defaultBucketSelection', () => {
  it('prefers the data bucket, then the first allowed bucket, else none', () => {
    const buckets = [{ name: 'a', allowed: false }, { name: 'b', allowed: true }, { name: 'c', allowed: true }];
    expect(defaultBucketSelection(buckets, 'c')).toBe('c');
    expect(defaultBucketSelection(buckets, null)).toBe('b');
    expect(defaultBucketSelection([{ name: 'a', allowed: false }, { name: 'd' }], null)).toBeNull();
    expect(defaultBucketSelection([], null)).toBeNull();
  });
});

describe('bucket marking', () => {
  it('marks each bucket Allowed / Not allowed / Portal-created and explains the Allowed badge', async () => {
    listDataBuckets.mockResolvedValue(
      bucketsResponse(
        [
          { name: CONFIGURED, region: 'us-east-1', is_configured: true, allowed: true },
          { name: FOREIGN, region: 'us-east-1', allowed: false, portal_created: true },
        ],
        null
      )
    );
    // Nothing is allowed... except the configured bucket, so the page opens
    // it; open the bucket selector to see every row.
    renderPage();
    await waitFor(() => expect(listDataFolders).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: /Change Bucket/i }));

    const table = within(await screen.findByTestId('data-bucket-selector-table'));
    expect(table.getByText('Allowed')).toBeInTheDocument();
    expect(table.getByText('Not allowed')).toBeInTheDocument();
    expect(table.getByText('Portal-created')).toBeInTheDocument();

    // The Allowed badge is a popover trigger; its content names the exception.
    fireEvent.click(table.getByText('Allowed'));
    expect(await screen.findByText(ALLOWED_BADGE_TOOLTIP)).toBeInTheDocument();
  });
});

describe('the inline allow action', () => {
  const twoBuckets = (hasDataRole = false) =>
    bucketsResponse(
      [
        { name: CONFIGURED, region: 'us-east-1', is_configured: true, allowed: false },
        { name: FOREIGN, region: 'us-east-1', allowed: false },
      ],
      null,
      hasDataRole
    );

  it('is visible to a UseCaseAdmin on not-allowed buckets and refreshes on success', async () => {
    listDataBuckets.mockResolvedValue(twoBuckets());
    allowBucketForUsecase.mockResolvedValue({
      message: 'ok',
      added: [FOREIGN],
      already_present: [],
      removed: [],
      not_present: [],
      allowed_s3_buckets: [CONFIGURED, FOREIGN],
    });
    renderPage();

    // No bucket is allowed: the page stays on the selection view.
    await screen.findByText('Select a Data Bucket');
    await waitFor(() => expect(actionButtons()).toHaveLength(2));
    expect(listDataFolders).not.toHaveBeenCalled();

    const callsBefore = { usecase: getUseCase.mock.calls.length, buckets: listDataBuckets.mock.calls.length };
    fireEvent.click(actionButtons()[1]);

    await waitFor(() => expect(allowBucketForUsecase).toHaveBeenCalledWith(USECASE_ID, FOREIGN));
    await waitFor(() => {
      expect(getUseCase.mock.calls.length).toBeGreaterThan(callsBefore.usecase);
      expect(listDataBuckets.mock.calls.length).toBeGreaterThan(callsBefore.buckets);
    });
  });

  it('is hidden from a Viewer', async () => {
    grantRole('Viewer');
    listDataBuckets.mockResolvedValue(twoBuckets());
    renderPage();

    await screen.findByText('Select a Data Bucket');
    await waitFor(() => expect(getMyUsecases).toHaveBeenCalled());
    await settle();
    expect(actionButtons()).toHaveLength(0);
    expect(cards().queryByText(DATA_ACCOUNT_CANNOT_ADD_MESSAGE)).not.toBeInTheDocument();
  });

  it('is replaced by the Data Account note when has_data_account_role is true', async () => {
    listDataBuckets.mockResolvedValue(twoBuckets(true));
    renderPage();

    await screen.findByText('Select a Data Bucket');
    await waitFor(() => expect(getMyUsecases).toHaveBeenCalled());
    await settle();
    expect(actionButtons()).toHaveLength(0);
    expect(cards().getAllByText(DATA_ACCOUNT_CANNOT_ADD_MESSAGE)).toHaveLength(2);
  });

  it('renders a refused allow through the notice', async () => {
    listDataBuckets.mockResolvedValue(twoBuckets());
    allowBucketForUsecase.mockRejectedValue(
      bucketError('bucket_not_eligible', { bucket: FOREIGN, reason: 'no_tags', remedy: 'tag it first' })
    );
    renderPage();

    await screen.findByText('Select a Data Bucket');
    await waitFor(() => expect(actionButtons()).toHaveLength(2));
    fireEvent.click(actionButtons()[1]);

    expect(await screen.findByText(`Bucket ${FOREIGN} is not tagged for the portal`)).toBeInTheDocument();
    expect(screen.getByText(/aws s3control tag-resource/)).toBeInTheDocument();
    expect(screen.getByText('tag it first')).toBeInTheDocument();
  });
});

describe('default selection', () => {
  it('opens the first allowed bucket when the use case has no data bucket', async () => {
    listDataBuckets.mockResolvedValue(
      bucketsResponse(
        [
          { name: FOREIGN, region: 'us-east-1', allowed: false },
          { name: 'second-allowed', region: 'us-east-1', allowed: true },
          { name: 'third-allowed', region: 'us-east-1', allowed: true },
        ],
        null
      )
    );
    renderPage();

    await waitFor(() => expect(listDataFolders).toHaveBeenCalledTimes(1));
    expect(listDataFolders).toHaveBeenCalledWith(USECASE_ID, { bucket: 'second-allowed', prefix: '' });
  });

  it('opens the data bucket when there is one', async () => {
    listDataBuckets.mockResolvedValue(
      bucketsResponse(
        [
          { name: FOREIGN, region: 'us-east-1', allowed: true },
          { name: CONFIGURED, region: 'us-east-1', is_configured: true, allowed: true },
        ],
        CONFIGURED
      )
    );
    renderPage();

    await waitFor(() => expect(listDataFolders).toHaveBeenCalledTimes(1));
    expect(listDataFolders).toHaveBeenCalledWith(USECASE_ID, { bucket: CONFIGURED, prefix: '' });
  });

  it('selects nothing when no bucket is allowed, so the page never opens onto a refusal', async () => {
    listDataBuckets.mockResolvedValue(
      bucketsResponse([{ name: FOREIGN, region: 'us-east-1', allowed: false }], null)
    );
    renderPage();

    await screen.findByText('Select a Data Bucket');
    await settle();
    expect(listDataFolders).not.toHaveBeenCalled();
    expect(cards().getByText(FOREIGN)).toBeInTheDocument();
  });
});

describe('a refused folder listing', () => {
  it('renders the notice in place of the folder table and reloads after the allow', async () => {
    listDataBuckets.mockResolvedValue(
      bucketsResponse(
        [{ name: CONFIGURED, region: 'us-east-1', is_configured: true, allowed: true }],
        CONFIGURED
      )
    );
    listDataFolders
      .mockRejectedValueOnce(bucketError('bucket_not_allowed', { bucket: CONFIGURED, remedy: 'remedy' }))
      .mockResolvedValue({ bucket: CONFIGURED, prefix: '', folders: [], files: [], is_truncated: false });
    allowBucketForUsecase.mockResolvedValue({
      message: 'ok',
      added: [CONFIGURED],
      already_present: [],
      removed: [],
      not_present: [],
      allowed_s3_buckets: [CONFIGURED],
    });
    renderPage();

    expect(
      await screen.findByText(`Bucket ${CONFIGURED} is not allowed for this use case`)
    ).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Filter files...')).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: ALLOW_ACTION_LABEL }));

    await waitFor(() => expect(allowBucketForUsecase).toHaveBeenCalledWith(USECASE_ID, CONFIGURED));
    await waitFor(() => expect(listDataFolders).toHaveBeenCalledTimes(2));
    expect(await screen.findByPlaceholderText('Filter files...')).toBeInTheDocument();
    expect(
      screen.queryByText(`Bucket ${CONFIGURED} is not allowed for this use case`)
    ).not.toBeInTheDocument();
  });
});
