/**
 * Component tests for `RegisterDeviceDialog`
 * (station-quick-setup task 9.5, Requirements 1.2, 1.7, 1.8).
 *
 * Covers the per-field validation feedback required by the design's
 * frontend section: the device-name and Device_Group fields each validate
 * against the IoT name pattern and surface a per-field message, the
 * Device_Group autocomplete is populated from the Use_Case's existing IoT
 * Thing Groups while still accepting a free-text new group name, and a valid
 * submission posts trimmed values and hands the created registration back to
 * the caller.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import RegisterDeviceDialog, { IOT_NAME_PATTERN } from './RegisterDeviceDialog';
import { ApiError, RegistrationWithCommand } from '../services/api';

const { registerDevice, listThingGroups } = vi.hoisted(() => ({
  registerDevice: vi.fn(),
  listThingGroups: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: { registerDevice, listThingGroups },
  };
});

const REQUIRED = 'This field is required.';
const PATTERN_HINT =
  'Use 1–128 characters: letters, digits, colon (:), underscore (_), or hyphen (-).';

function result(): RegistrationWithCommand {
  return {
    registration: {
      registration_id: 'reg-1',
      usecase_id: 'uc-1',
      device_name: 'station-42',
      device_group: 'Line3_Group',
      status: 'pending',
      created_by: 'user-1',
      created_at: 1730000000,
      updated_at: 1730000000,
      token_expires_at: 1730005400,
    },
    setup_command: 'curl -fsSL https://x/quick-setup/bootstrap | sudo bash',
    token_expires_at: 1730005400,
  };
}

function renderDialog(overrides: Partial<React.ComponentProps<typeof RegisterDeviceDialog>> = {}) {
  const onDismiss = vi.fn();
  const onRegistered = vi.fn();
  render(
    <RegisterDeviceDialog
      visible
      usecaseId="uc-1"
      onDismiss={onDismiss}
      onRegistered={onRegistered}
      {...overrides}
    />
  );
  return { onDismiss, onRegistered };
}

/** Find the device-name text input via its aria-label. */
function deviceNameInput(): HTMLInputElement {
  return screen.getByRole('textbox', { name: 'Device name' }) as HTMLInputElement;
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: 'Register device' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  listThingGroups.mockResolvedValue({ thing_groups: ['Line3_Group', 'ExistingGroup'], count: 2 });
  registerDevice.mockResolvedValue(result());
});

describe('IOT_NAME_PATTERN', () => {
  it('accepts valid IoT names and rejects names with disallowed characters or length', () => {
    expect(IOT_NAME_PATTERN.test('station-42')).toBe(true);
    expect(IOT_NAME_PATTERN.test('Line3_Group:sub')).toBe(true);
    expect(IOT_NAME_PATTERN.test('bad name')).toBe(false); // space
    expect(IOT_NAME_PATTERN.test('bad!')).toBe(false); // punctuation
    expect(IOT_NAME_PATTERN.test('')).toBe(false); // empty
    expect(IOT_NAME_PATTERN.test('a'.repeat(129))).toBe(false); // too long
  });
});

describe('RegisterDeviceDialog per-field validation (Requirements 1.2, 1.9)', () => {
  it('identifies both missing fields on an empty submit and does not register', async () => {
    renderDialog();
    submit();

    // Both the device-name and Device_Group fields report the required error.
    await waitFor(() => {
      expect(screen.getAllByText(REQUIRED)).toHaveLength(2);
    });
    expect(registerDevice).not.toHaveBeenCalled();
  });

  it('shows a per-field pattern message and marks the device-name field invalid', async () => {
    renderDialog();
    const input = deviceNameInput();
    fireEvent.change(input, { target: { value: 'bad name!' } });

    // The pattern hint is shown as the field error (in addition to the
    // always-present constraint hint), and the input is flagged invalid.
    await waitFor(() => {
      expect(screen.getAllByText(PATTERN_HINT).length).toBeGreaterThanOrEqual(2);
    });
    expect(input).toHaveAttribute('aria-invalid', 'true');

    submit();
    expect(registerDevice).not.toHaveBeenCalled();
  });

  it('validates the Device_Group field independently of the device name', async () => {
    renderDialog();
    // Valid device name, invalid group.
    fireEvent.change(deviceNameInput(), { target: { value: 'station-42' } });
    const group = createWrapper(document.body).findAutosuggest()!;
    group.setInputValue('bad group!');

    // Device name is valid (Cloudscape omits aria-invalid when the field is
    // valid), so only the group is invalid and the submit is blocked.
    expect(deviceNameInput()).not.toHaveAttribute('aria-invalid');
    submit();
    await waitFor(() => expect(registerDevice).not.toHaveBeenCalled());
  });
});

describe('RegisterDeviceDialog Device_Group autocomplete (Requirements 1.7, 1.8)', () => {
  it('loads the Use_Case existing Thing Groups for selection', async () => {
    renderDialog();
    await waitFor(() => expect(listThingGroups).toHaveBeenCalledWith('uc-1'));

    const group = createWrapper(document.body).findAutosuggest()!;
    group.focus();
    await waitFor(() => {
      expect(screen.getByText('Line3_Group')).toBeInTheDocument();
      expect(screen.getByText('ExistingGroup')).toBeInTheDocument();
    });
  });

  it('accepts a free-text new group name and submits trimmed values', async () => {
    const { onRegistered } = renderDialog();
    fireEvent.change(deviceNameInput(), { target: { value: '  station-42  ' } });
    const group = createWrapper(document.body).findAutosuggest()!;
    group.setInputValue('  NewLineGroup  ');

    submit();

    await waitFor(() =>
      expect(registerDevice).toHaveBeenCalledWith({
        device_name: 'station-42',
        device_group: 'NewLineGroup',
        usecase_id: 'uc-1',
      })
    );
    await waitFor(() => expect(onRegistered).toHaveBeenCalledTimes(1));
  });
});

describe('RegisterDeviceDialog submission errors', () => {
  it('surfaces a 409 conflict on the device-name field (Requirement 1.3)', async () => {
    registerDevice.mockRejectedValue(
      new ApiError('Device name already exists', 409)
    );
    const { onRegistered } = renderDialog();
    fireEvent.change(deviceNameInput(), { target: { value: 'station-42' } });
    const group = createWrapper(document.body).findAutosuggest()!;
    group.setInputValue('Line3_Group');

    submit();

    expect(await screen.findByText('Device name already exists')).toBeInTheDocument();
    expect(onRegistered).not.toHaveBeenCalled();
  });

  it('disables submission when no Use_Case is selected', () => {
    renderDialog({ usecaseId: null });
    const button = screen.getByRole('button', { name: 'Register device' });
    expect(
      button.hasAttribute('disabled') ||
        button.getAttribute('aria-disabled') === 'true'
    ).toBe(true);
  });
});
