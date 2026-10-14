/**
 * S3BucketPicker advisory marking and PortalAdmin-only bucket creation
 * (spec: portal-usecase-bucket-config, design §10.2, tasks 7.3 / 7.5).
 *
 * Through the rendered picker with `apiService` mocked:
 * - each row is marked from the server's advisory `eligible` /
 *   `portal_created` flags, and the "Not tagged for the portal" badge's
 *   tooltip says tag discovery is regional;
 * - "Create new bucket" is rendered for a PortalAdmin only;
 * - a failed create is rendered by code: `bucket_already_exists`,
 *   `invalid_bucket_name`, and a 403 as the PortalAdmin message;
 * - a successful create refreshes the list and selects the new bucket.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import S3BucketPicker, {
  CREATE_BUCKET_FORBIDDEN_MESSAGE,
  NOT_TAGGED_TOOLTIP,
  createBucketErrorMessage,
} from './S3BucketPicker';
import { ApiError } from '../services/api';

const { listS3Buckets, createS3Bucket, getMyUsecases, useAuthMock } = vi.hoisted(() => ({
  listS3Buckets: vi.fn(),
  createS3Bucket: vi.fn(),
  getMyUsecases: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: { listS3Buckets, createS3Bucket, getMyUsecases },
  };
});

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../contexts/AuthContext')>();
  return { ...actual, useAuth: useAuthMock };
});

const BUCKETS = [
  { name: 'tagged-bucket', region: 'us-east-1', creation_date: null, eligible: true, portal_created: true },
  { name: 'untagged-bucket', region: 'us-east-1', creation_date: null, eligible: false, portal_created: false },
  { name: 'legacy-bucket', region: 'us-east-1', creation_date: null },
];

function setPortalAdmin(isSuperUser: boolean) {
  getMyUsecases.mockResolvedValue({ usecases: [], total_count: 0, is_super_user: isSuperUser });
}

function apiError(status: number, body: Record<string, unknown>): ApiError {
  return new ApiError(String(body.error || `HTTP ${status}`), status, undefined, body);
}

function renderPicker(onSelect = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <S3BucketPicker selectedBucket="" onSelect={onSelect} region="us-east-1" />
    </QueryClientProvider>
  );
  return onSelect;
}

const createButton = () => screen.queryByRole('button', { name: 'Create new bucket' });

async function settle() {
  await waitFor(() => expect(getMyUsecases).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function openCreateForm(name: string) {
  fireEvent.click(await screen.findByRole('button', { name: 'Create new bucket' }));
  fireEvent.change(await screen.findByPlaceholderText('new-bucket-name'), { target: { value: name } });
  fireEvent.click(screen.getByRole('button', { name: 'Create' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'UseCaseAdmin' } });
  listS3Buckets.mockResolvedValue({ buckets: BUCKETS, count: BUCKETS.length });
  setPortalAdmin(false);
});

describe('advisory marking', () => {
  it('marks tagged, untagged and portal-created buckets', async () => {
    renderPicker();

    expect(await screen.findByText('tagged-bucket')).toBeInTheDocument();
    expect(screen.getByText('Tagged for the portal')).toBeInTheDocument();
    expect(screen.getByText('Portal-created')).toBeInTheDocument();
    expect(screen.getByText('Not tagged for the portal')).toBeInTheDocument();
  });

  it('explains that tag discovery is regional on the not-tagged badge', async () => {
    renderPicker();

    fireEvent.click(await screen.findByText('Not tagged for the portal'));
    expect(await screen.findByText(NOT_TAGGED_TOOLTIP)).toBeInTheDocument();
  });
});

describe('Create new bucket', () => {
  it('is hidden for a non-PortalAdmin', async () => {
    renderPicker();

    await screen.findByText('tagged-bucket');
    await settle();
    expect(createButton()).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument();
  });

  it('is rendered for a PortalAdmin', async () => {
    setPortalAdmin(true);
    renderPicker();

    await waitFor(() => expect(createButton()).toBeInTheDocument());
  });

  it('creates the bucket, refreshes the list and selects it', async () => {
    setPortalAdmin(true);
    createS3Bucket.mockResolvedValue({
      bucket: {
        name: 'brand-new',
        region: 'us-east-1',
        portal_created: true,
        tags: ['dda-portal:managed', 'dda-portal:created-by'],
        abac: 'enabled',
        added_to_usecase: false,
      },
    });
    const onSelect = renderPicker();
    await screen.findByText('tagged-bucket');

    await openCreateForm('brand-new');

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('brand-new'));
    expect(createS3Bucket).toHaveBeenCalledWith({ name: 'brand-new', region: 'us-east-1' });
    expect(listS3Buckets).toHaveBeenCalledTimes(2);
  });

  it('renders bucket_already_exists with the server remedy', async () => {
    setPortalAdmin(true);
    createS3Bucket.mockRejectedValue(
      apiError(409, {
        error: 'Bucket already exists',
        error_code: 'bucket_already_exists',
        bucket: 'taken',
        remedy: 'Select the existing bucket instead.',
      })
    );
    renderPicker();
    await screen.findByText('tagged-bucket');

    await openCreateForm('taken');

    expect(
      await screen.findByText('Bucket taken already exists in this account. Select the existing bucket instead.')
    ).toBeInTheDocument();
  });

  it('renders invalid_bucket_name with the grammar', async () => {
    setPortalAdmin(true);
    createS3Bucket.mockRejectedValue(
      apiError(400, { error: 'Invalid bucket name', error_code: 'invalid_bucket_name', bucket: 'Bad_Name' })
    );
    renderPicker();
    await screen.findByText('tagged-bucket');

    await openCreateForm('Bad_Name');

    expect(await screen.findByText(/Bad_Name is not a valid bucket name/)).toBeInTheDocument();
  });

  it('renders a 403 as the PortalAdmin message (stale permissions answer)', async () => {
    setPortalAdmin(true);
    createS3Bucket.mockRejectedValue(
      apiError(403, { error: 'Only a PortalAdmin can create a bucket without a use case' })
    );
    renderPicker();
    await screen.findByText('tagged-bucket');

    await openCreateForm('late-bucket');

    expect(await screen.findByText(CREATE_BUCKET_FORBIDDEN_MESSAGE)).toBeInTheDocument();
  });
});

describe('createBucketErrorMessage', () => {
  it('maps each code and falls back to the message', () => {
    expect(
      createBucketErrorMessage(apiError(409, { error: 'x', error_code: 'bucket_already_exists' }), 'b')
    ).toContain('already exists');
    expect(
      createBucketErrorMessage(apiError(400, { error: 'x', error_code: 'invalid_bucket_name' }), 'B')
    ).toContain('not a valid bucket name');
    expect(createBucketErrorMessage(apiError(403, { error: 'x' }), 'b')).toBe(CREATE_BUCKET_FORBIDDEN_MESSAGE);
    expect(createBucketErrorMessage(apiError(500, { error: 'boom' }), 'b')).toBe('boom');
    expect(createBucketErrorMessage(new Error('plain'), 'b')).toBe('plain');
    expect(createBucketErrorMessage('nope', 'b')).toBe('Failed to create bucket');
  });
});
