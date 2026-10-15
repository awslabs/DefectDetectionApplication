/**
 * S3BucketPicker: the Registration_Report remedy of a created bucket (spec:
 * portal-authorization-and-iam-scoping, task 8.1; design I5).
 *
 * A PortalAdmin creates a bucket; when the 201 `bucket` carries a `remedy`
 * (the portal could not reach the bucket yet), the remedy is shown beside
 * the created bucket's name; when `remedy` is null nothing extra is shown.
 * The create still selects the new bucket either way.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import S3BucketPicker from './S3BucketPicker';

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

const REMEDY = 'Redeploy the portal so its roles reach this bucket';

function createdBucket(remedy: string | null) {
  return {
    bucket: {
      name: 'new-bucket',
      region: 'us-east-1',
      portal_created: true,
      tags: ['dda-portal:managed', 'dda-portal:created-by'],
      abac: 'enabled',
      added_to_usecase: false,
      tagged: true,
      abac_enabled: true,
      iam_reachable: remedy === null,
      remedy,
    },
  };
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

async function createBucket(name: string) {
  await waitFor(() => expect(getMyUsecases).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  fireEvent.click(await screen.findByRole('button', { name: 'Create new bucket' }));
  fireEvent.change(await screen.findByPlaceholderText('new-bucket-name'), { target: { value: name } });
  fireEvent.click(screen.getByRole('button', { name: 'Create' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'PortalAdmin' } });
  listS3Buckets.mockResolvedValue({ buckets: [], count: 0 });
  getMyUsecases.mockResolvedValue({ usecases: [], total_count: 0, is_super_user: true });
});

describe('the created bucket remedy', () => {
  it('is shown beside the created bucket when the report names one', async () => {
    createS3Bucket.mockResolvedValue(createdBucket(REMEDY));
    const onSelect = renderPicker();
    await createBucket('new-bucket');
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('new-bucket'));
    expect(
      await screen.findByText('Bucket new-bucket was created; it needs one more step')
    ).toBeInTheDocument();
    expect(screen.getByText(REMEDY, { exact: false })).toBeInTheDocument();
  });

  it('is not shown when the bucket is reachable', async () => {
    createS3Bucket.mockResolvedValue(createdBucket(null));
    const onSelect = renderPicker();
    await createBucket('new-bucket');
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('new-bucket'));
    expect(screen.queryByText(/needs one more step/i)).not.toBeInTheDocument();
  });
});
