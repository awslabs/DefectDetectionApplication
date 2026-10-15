/**
 * Use case Edit modal "Allowed buckets" section (spec:
 * portal-usecase-bucket-config, design §10.2, tasks 7.4 / 7.5).
 *
 * The section lists the configured buckets (read-only) and the additional
 * buckets (each with Remove), and allows another bucket through
 * PUT /usecases/{id} {additional_s3_buckets_add: [name]} /
 * {additional_s3_buckets_remove: [name]}; the list is refreshed from the
 * server's `allowed_s3_buckets`. Errors render by code: a 400
 * `bucket_not_eligible` through the notice (tag command), a 403 as the
 * UseCaseAdmin message, 409 `concurrent_update` and 404 `usecase_not_found`
 * as their messages. The modal's existing `data_s3_bucket || ''` submission
 * on "Save Changes" is unchanged and never carries the bucket list fields.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import UseCases, {
  additionalFromAllowed,
  bucketUpdateErrorMessage,
  configuredBuckets,
} from './UseCases';
import { USECASE_ADMIN_REQUIRED_MESSAGE } from '../components/BucketNotAllowedNotice';
import { ApiError } from '../services/api';
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

function usecase(overrides: Partial<UseCase> = {}): UseCase {
  return {
    usecase_id: 'uc-1',
    name: 'Line 3',
    account_id: 'acct-1',
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

function putResponse(allowed: string[], extra: Record<string, string[]> = {}) {
  return {
    message: 'Use case updated successfully',
    added: [],
    already_present: [],
    removed: [],
    not_present: [],
    ...extra,
    allowed_s3_buckets: allowed,
  };
}

function apiError(status: number, body: Record<string, unknown>): ApiError {
  return new ApiError(String(body.error || `HTTP ${status}`), status, undefined, body);
}

async function openEditForm(item: UseCase) {
  listUseCases.mockResolvedValue({ usecases: [item] });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UseCases />
      </MemoryRouter>
    </QueryClientProvider>
  );
  await screen.findByText('Line 3');
  const actionButtons = screen.getAllByRole('button', { name: /Actions/i });
  fireEvent.click(actionButtons[actionButtons.length - 1]);
  fireEvent.click(await screen.findByText('Edit'));
  const header = await screen.findByText('Edit Use Case');
  const dialog = header.closest('[role="dialog"]') as HTMLElement;
  return within(dialog);
}

function allowInput(dialog: ReturnType<typeof within>) {
  return dialog.getByPlaceholderText('another-bucket');
}

async function allowBucket(dialog: ReturnType<typeof within>, name: string) {
  fireEvent.change(allowInput(dialog), { target: { value: name } });
  const button = dialog.getByRole('button', { name: 'Allow' });
  await waitFor(() => expect(button).not.toBeDisabled());
  fireEvent.click(button);
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'UseCaseAdmin' } });
  updateUseCase.mockResolvedValue({ message: 'Use case updated successfully' });
  getSharedComponentsStatus.mockResolvedValue({
    latest_version: '1.0.0',
    total_usecases: 1,
    usecases_needing_update: 0,
    usecases: [],
  });
  getMyUsecases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line 3', user_role: 'UseCaseAdmin' }],
    total_count: 1,
    is_super_user: false,
  });
});

describe('pure helpers', () => {
  it('lists the configured buckets once each', () => {
    expect(configuredBuckets({ s3_bucket: 'a', data_s3_bucket: 'b' })).toEqual(['a', 'b']);
    expect(configuredBuckets({ s3_bucket: 'a', data_s3_bucket: 'a' })).toEqual(['a']);
    expect(configuredBuckets({ s3_bucket: 'a', data_s3_bucket: '' })).toEqual(['a']);
  });

  it('derives the additional list from the allowed set minus the configured buckets', () => {
    expect(
      additionalFromAllowed({ allowed_s3_buckets: ['a', 'b', 'x', 'y'] }, { s3_bucket: 'a', data_s3_bucket: 'b' })
    ).toEqual(['x', 'y']);
    expect(additionalFromAllowed({ allowed_s3_buckets: [] }, { s3_bucket: 'a' })).toEqual([]);
  });

  it('maps the non-notice error codes to messages', () => {
    expect(bucketUpdateErrorMessage(apiError(403, { error: 'Insufficient permissions' }))).toBe(
      USECASE_ADMIN_REQUIRED_MESSAGE
    );
    expect(
      bucketUpdateErrorMessage(apiError(404, { error: 'Use case not found', error_code: 'usecase_not_found' }))
    ).toBe('This use case no longer exists');
    expect(
      bucketUpdateErrorMessage(apiError(409, { error: 'conflict', error_code: 'concurrent_update' }))
    ).toContain('retry');
    expect(bucketUpdateErrorMessage(apiError(500, { error: 'Failed to update use case' }))).toBe(
      'Failed to update use case'
    );
    expect(bucketUpdateErrorMessage(new Error('plain'))).toBe('plain');
  });
});

describe('the Allowed buckets section', () => {
  it('lists the configured buckets read-only and the additional buckets with Remove', async () => {
    const dialog = await openEditForm(
      usecase({ data_s3_bucket: 'line3-data', additional_s3_buckets: ['extra-1', 'extra-2'] })
    );

    expect(dialog.getByText('Allowed buckets')).toBeInTheDocument();
    expect(dialog.getByText('Configured buckets')).toBeInTheDocument();
    expect(dialog.getByText('line3-data')).toBeInTheDocument();
    expect(dialog.getByText('extra-1')).toBeInTheDocument();
    expect(dialog.getByText('extra-2')).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: 'Remove extra-1' })).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: 'Remove extra-2' })).toBeInTheDocument();
    // Configured buckets carry no Remove.
    expect(dialog.queryByRole('button', { name: 'Remove line3-bucket' })).not.toBeInTheDocument();
    expect(dialog.queryByRole('button', { name: 'Remove line3-data' })).not.toBeInTheDocument();
  });

  it('shows the empty state when the use case has no additional buckets', async () => {
    const dialog = await openEditForm(usecase());
    expect(dialog.getByText('No additional buckets')).toBeInTheDocument();
  });

  it('allows another bucket through the add body and refreshes from allowed_s3_buckets', async () => {
    allowBucketForUsecase.mockResolvedValue(
      putResponse(['line3-bucket', 'new-bucket'], { added: ['new-bucket'] })
    );
    const dialog = await openEditForm(usecase());

    await allowBucket(dialog, 'new-bucket');

    await waitFor(() => expect(allowBucketForUsecase).toHaveBeenCalledWith('uc-1', 'new-bucket'));
    expect(await dialog.findByText('new-bucket')).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: 'Remove new-bucket' })).toBeInTheDocument();
    expect(dialog.queryByText('No additional buckets')).not.toBeInTheDocument();
    expect(allowInput(dialog)).toHaveValue('');
    // Nothing else was written: the edit form itself was not submitted.
    expect(updateUseCase).not.toHaveBeenCalled();
  });

  it('refuses a malformed name locally without calling the API', async () => {
    const dialog = await openEditForm(usecase());

    fireEvent.change(allowInput(dialog), { target: { value: 'Bad_Name' } });

    expect(dialog.getByRole('button', { name: 'Allow' })).toBeDisabled();
    expect(allowBucketForUsecase).not.toHaveBeenCalled();
  });

  it('removes an additional bucket through the remove body', async () => {
    removeAllowedBucket.mockResolvedValue(putResponse(['line3-bucket', 'extra-2'], { removed: ['extra-1'] }));
    const dialog = await openEditForm(usecase({ additional_s3_buckets: ['extra-1', 'extra-2'] }));

    fireEvent.click(dialog.getByRole('button', { name: 'Remove extra-1' }));

    await waitFor(() => expect(removeAllowedBucket).toHaveBeenCalledWith('uc-1', 'extra-1'));
    await waitFor(() => expect(dialog.queryByText('extra-1')).not.toBeInTheDocument());
    expect(dialog.getByText('extra-2')).toBeInTheDocument();
  });

  it('renders a refused add (bucket_not_eligible) through the notice', async () => {
    allowBucketForUsecase.mockRejectedValue(
      apiError(400, {
        error: 'Bucket is not eligible',
        error_code: 'bucket_not_eligible',
        bucket: 'raw-bucket',
        reason: 'tag_missing',
        remedy: 'tag it',
      })
    );
    const dialog = await openEditForm(usecase());

    await allowBucket(dialog, 'raw-bucket');

    expect(await dialog.findByText('Bucket raw-bucket is not tagged for the portal')).toBeInTheDocument();
    expect(dialog.getByText(/aws s3control tag-resource/)).toBeInTheDocument();
    expect(dialog.getByText('No additional buckets')).toBeInTheDocument();
  });

  it('renders a 403 as the UseCaseAdmin message', async () => {
    allowBucketForUsecase.mockRejectedValue(apiError(403, { error: 'Insufficient permissions to update use case' }));
    const dialog = await openEditForm(usecase());

    await allowBucket(dialog, 'new-bucket');

    expect(await dialog.findByText(USECASE_ADMIN_REQUIRED_MESSAGE)).toBeInTheDocument();
  });

  it('renders concurrent_update and usecase_not_found by code', async () => {
    removeAllowedBucket
      .mockRejectedValueOnce(
        apiError(409, { error: 'Concurrent update', error_code: 'concurrent_update', bucket: 'extra-1' })
      )
      .mockRejectedValueOnce(apiError(404, { error: 'Use case not found', error_code: 'usecase_not_found' }));
    const dialog = await openEditForm(usecase({ additional_s3_buckets: ['extra-1'] }));

    fireEvent.click(dialog.getByRole('button', { name: 'Remove extra-1' }));
    expect(
      await dialog.findByText('The use case was changed by someone else at the same time; retry')
    ).toBeInTheDocument();

    fireEvent.click(dialog.getByRole('button', { name: 'Remove extra-1' }));
    expect(await dialog.findByText('This use case no longer exists')).toBeInTheDocument();
    // The list is untouched by a failed remove.
    expect(dialog.getByText('extra-1')).toBeInTheDocument();
  });

  it('leaves the data_s3_bucket || "" submission unchanged and never sends the list fields', async () => {
    const dialog = await openEditForm(usecase({ additional_s3_buckets: ['extra-1'] }));

    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(updateUseCase).toHaveBeenCalledTimes(1));
    const [id, data] = updateUseCase.mock.calls[0];
    expect(id).toBe('uc-1');
    expect(data.data_s3_bucket).toBe('');
    expect(data.s3_bucket).toBe('line3-bucket');
    expect('additional_s3_buckets' in data).toBe(false);
    expect('additional_s3_buckets_add' in data).toBe(false);
    expect('additional_s3_buckets_remove' in data).toBe(false);
    expect('allowed_s3_buckets' in data).toBe(false);
  });
});
