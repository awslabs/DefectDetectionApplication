/**
 * Import Model role gate and bucket refusal (spec:
 * portal-usecase-bucket-config, design §7 / §10.2, tasks 7.4 / 7.5).
 *
 * POST /models/validate now requires the DataScientist role on the use case,
 * and the page is mounted without a route guard, so a Viewer reaches it and
 * receives a 403 where validation used to run: the page renders that 403 as
 * "Validating and importing models requires the DataScientist role on this
 * use case" instead of the generic message (no route guard is added; the
 * server stays the authority). A refused model bucket (`bucket_not_allowed`)
 * is rendered through the notice with the allow action.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import ImportModel, { DATA_SCIENTIST_REQUIRED_MESSAGE } from './ImportModel';
import { ALLOW_ACTION_LABEL } from '../components/BucketNotAllowedNotice';
import { ApiError } from '../services/api';

const { listUseCases, validateModel, importModel, getMyUsecases, allowBucketForUsecase, useAuthMock } =
  vi.hoisted(() => ({
    listUseCases: vi.fn(),
    validateModel: vi.fn(),
    importModel: vi.fn(),
    getMyUsecases: vi.fn(),
    allowBucketForUsecase: vi.fn(),
    useAuthMock: vi.fn(),
  }));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: { listUseCases, validateModel, importModel, getMyUsecases, allowBucketForUsecase },
  };
});

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../contexts/AuthContext')>();
  return { ...actual, useAuth: useAuthMock };
});

const USECASE_ID = 'uc-1';
const MODEL_URI = 's3://other-team-bucket/models/model.tar.gz';

function apiError(status: number, body: Record<string, unknown>): ApiError {
  return new ApiError(String(body.error || `HTTP ${status}`), status, undefined, body);
}

async function renderAndValidate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ImportModel />
      </MemoryRouter>
    </QueryClientProvider>
  );
  await waitFor(() => expect(listUseCases).toHaveBeenCalled());

  const select = createWrapper(document.body).findSelect()!;
  await waitFor(() => {
    select.openDropdown();
    expect(select.findDropdown().findOptionByValue(USECASE_ID)).not.toBeNull();
  });
  select.selectOptionByValue(USECASE_ID);

  fireEvent.change(screen.getByPlaceholderText('s3://my-bucket/models/my-model.tar.gz'), {
    target: { value: MODEL_URI },
  });
  const validate = screen.getByRole('button', { name: 'Validate Model' });
  await waitFor(() => expect(validate).not.toBeDisabled());
  fireEvent.click(validate);
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'Viewer' } });
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'Line 3', account_id: 'acct-1' }],
    count: 1,
  });
  getMyUsecases.mockResolvedValue({
    usecases: [{ usecase_id: USECASE_ID, name: 'Line 3', user_role: 'Viewer' }],
    total_count: 1,
    is_super_user: false,
  });
});

describe('the DataScientist role gate', () => {
  it('renders a 403 from validation as the DataScientist message', async () => {
    validateModel.mockRejectedValue(apiError(403, { error: 'Insufficient permissions' }));

    await renderAndValidate();

    // Shown as the page alert and as the validation result.
    const messages = await screen.findAllByText(DATA_SCIENTIST_REQUIRED_MESSAGE);
    expect(messages.length).toBeGreaterThan(0);
    expect(screen.queryByText('Insufficient permissions')).not.toBeInTheDocument();
    expect(validateModel).toHaveBeenCalledWith({ usecase_id: USECASE_ID, model_s3_uri: MODEL_URI });
    expect(screen.getByRole('button', { name: 'Import Model' })).toBeDisabled();
  });

  it('still shows other validation failures verbatim', async () => {
    validateModel.mockRejectedValue(apiError(500, { error: 'Validation failed: artifact unreadable' }));

    await renderAndValidate();

    expect((await screen.findAllByText('Validation failed: artifact unreadable')).length).toBeGreaterThan(0);
    expect(screen.queryByText(DATA_SCIENTIST_REQUIRED_MESSAGE)).not.toBeInTheDocument();
  });
});

describe('a refused model bucket', () => {
  it('renders bucket_not_allowed through the notice instead of the generic alert', async () => {
    getMyUsecases.mockResolvedValue({
      usecases: [{ usecase_id: USECASE_ID, name: 'Line 3', user_role: 'UseCaseAdmin' }],
      total_count: 1,
      is_super_user: false,
    });
    validateModel.mockRejectedValue(
      apiError(400, {
        error: 'Bucket other-team-bucket is not allowed for this use case',
        error_code: 'bucket_not_allowed',
        bucket: 'other-team-bucket',
        usecase_id: USECASE_ID,
        purpose: 'validate-model',
        remedy: 'A UseCaseAdmin can allow this bucket for the use case.',
      })
    );

    await renderAndValidate();

    expect(
      await screen.findByText('Bucket other-team-bucket is not allowed for this use case')
    ).toBeInTheDocument();
    expect(screen.getByText('A UseCaseAdmin can allow this bucket for the use case.')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: ALLOW_ACTION_LABEL })).toBeInTheDocument();
    expect(screen.queryByText('Validation failed')).not.toBeInTheDocument();
  });
});
