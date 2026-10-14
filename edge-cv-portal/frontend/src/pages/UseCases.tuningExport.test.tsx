/**
 * Use_Case settings for VLM/LLM Anomaly Tuning Sample_Export
 * (spec: .kiro/specs/quality-prompt-tuning, task 5.1, Requirements 2.1, 2.9).
 *
 * The Use_Case edit form carries the "Tuning sample export" toggle and the
 * retention-days field; the values it saves are exactly the typed setting
 * pair `functions/usecases.py` validates (`tuning_sample_export` boolean,
 * `tuning_sample_retention_days` 7..365, default 30), and an out-of-range
 * retention is refused locally instead of being sent for a 400.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import UseCases, {
  TUNING_RETENTION_DEFAULT_DAYS,
  TUNING_RETENTION_MAX_DAYS,
  TUNING_RETENTION_MIN_DAYS,
  validateTuningRetentionDays,
} from './UseCases';
import { UseCase } from '../types';

const {
  listUseCases,
  updateUseCase,
  deleteUseCase,
  provisionSharedComponents,
  getSharedComponentsStatus,
  useAuthMock,
} = vi.hoisted(() => ({
  listUseCases: vi.fn(),
  updateUseCase: vi.fn(),
  deleteUseCase: vi.fn(),
  provisionSharedComponents: vi.fn(),
  getSharedComponentsStatus: vi.fn(),
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
    account_id: '123456789012',
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

function retentionInput(dialog: ReturnType<typeof within>) {
  return dialog.getByPlaceholderText(String(TUNING_RETENTION_DEFAULT_DAYS));
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
});

describe('retention validation (Requirement 2.9)', () => {
  it('accepts the documented range and an empty field', () => {
    for (const value of ['', '  ', '7', '30', '365', '120']) {
      expect(validateTuningRetentionDays(value)).toBeUndefined();
    }
    expect(TUNING_RETENTION_MIN_DAYS).toBe(7);
    expect(TUNING_RETENTION_MAX_DAYS).toBe(365);
    expect(TUNING_RETENTION_DEFAULT_DAYS).toBe(30);
  });

  it('refuses values outside the range and non-integers', () => {
    for (const value of ['6', '0', '366', '4000', '-1', '30.5', 'soon']) {
      expect(validateTuningRetentionDays(value)).toBeTruthy();
    }
  });
});

describe('the Use_Case settings form', () => {
  it('shows the export toggle reflecting the stored setting', async () => {
    const dialog = await openEditForm(usecase({
      tuning_sample_export: true,
      tuning_sample_retention_days: 45,
    }));

    const toggle = dialog.getByRole('checkbox');
    expect(toggle).toBeChecked();
    expect(dialog.getByText(/Enabled — devices export tuning samples/)).toBeTruthy();
    expect(retentionInput(dialog)).toHaveValue(45);
  });

  it('shows export disabled and no retention for a Use_Case that never opted in', async () => {
    const dialog = await openEditForm(usecase());

    expect(dialog.getByRole('checkbox')).not.toBeChecked();
    expect(dialog.getByText(/Disabled — devices export nothing/)).toBeTruthy();
    expect(retentionInput(dialog)).toHaveValue(null);
  });

  it('saves the enablement as a boolean and the retention as a number', async () => {
    const dialog = await openEditForm(usecase());

    fireEvent.click(dialog.getByRole('checkbox'));
    fireEvent.change(retentionInput(dialog), { target: { value: '90' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(updateUseCase).toHaveBeenCalledTimes(1));
    const [id, data] = updateUseCase.mock.calls[0];
    expect(id).toBe('uc-1');
    expect(data.tuning_sample_export).toBe(true);
    expect(data.tuning_sample_retention_days).toBe(90);
  });

  it('omits the retention when the field is left empty so the stored value stands', async () => {
    const dialog = await openEditForm(usecase({ tuning_sample_export: true }));

    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(updateUseCase).toHaveBeenCalledTimes(1));
    const [, data] = updateUseCase.mock.calls[0];
    expect(data.tuning_sample_export).toBe(true);
    expect('tuning_sample_retention_days' in data).toBe(false);
  });

  it('disabling saves false rather than dropping the setting', async () => {
    const dialog = await openEditForm(usecase({
      tuning_sample_export: true,
      tuning_sample_retention_days: 30,
    }));

    fireEvent.click(dialog.getByRole('checkbox'));
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(updateUseCase).toHaveBeenCalledTimes(1));
    expect(updateUseCase.mock.calls[0][1].tuning_sample_export).toBe(false);
  });

  it('refuses an out-of-range retention without calling the API', async () => {
    const dialog = await openEditForm(usecase());

    fireEvent.change(retentionInput(dialog), { target: { value: '400' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Save Changes' }));

    // Shown both as the form-field error and as the modal's error alert.
    expect(
      await screen.findAllByText(
        `Retention must be between ${TUNING_RETENTION_MIN_DAYS} and ${TUNING_RETENTION_MAX_DAYS} days`
      )
    ).not.toHaveLength(0);
    expect(updateUseCase).not.toHaveBeenCalled();
  });
});
