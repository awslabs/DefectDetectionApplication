/**
 * Component tests for the Auto-Labeling settings section (Settings →
 * Auto-Labeling): the form is shown only to PortalAdmin, loads the saved
 * Images per worker run (blank when nothing is saved), validates a whole
 * number within the bounds client-side, saves a number or null for a blank
 * field, and reports load and save errors.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import AutoLabelingSettings from './AutoLabelingSettings';

const { getAutoLabelingConfiguration, updateAutoLabelingConfiguration, useAuthMock } = vi.hoisted(() => ({
  getAutoLabelingConfiguration: vi.fn(),
  updateAutoLabelingConfiguration: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', () => ({
  apiService: { getAutoLabelingConfiguration, updateAutoLabelingConfiguration },
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

const BOUNDS = { default_images_per_run: 5, min_images_per_run: 1, max_images_per_run: 5 };
const RANGE_ERROR = 'Images per worker run must be a whole number from 1 to 5, or blank for the default';

beforeEach(() => {
  vi.clearAllMocks();
  getAutoLabelingConfiguration.mockResolvedValue({ images_per_run: 3, ...BOUNDS });
  updateAutoLabelingConfiguration.mockImplementation(async (config: { images_per_run: number | null }) => ({
    message: 'Auto-labeling configuration updated successfully',
    images_per_run: config.images_per_run,
  }));
});

function setAuthRole(role: string | null) {
  useAuthMock.mockReturnValue({ user: role ? { role } : null });
}

const field = () => screen.getByLabelText('Images per worker run');
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));

async function renderLoaded(expectedValue: number | null) {
  const result = render(<AutoLabelingSettings />);
  await waitFor(() => {
    expect(field()).toHaveValue(expectedValue);
  });
  return result;
}

describe('AutoLabelingSettings', () => {
  it('shows the saved Images per worker run and the Save button for PortalAdmin', async () => {
    setAuthRole('PortalAdmin');
    const { container } = await renderLoaded(3);

    expect(getAutoLabelingConfiguration).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Save Configuration' })).toBeInTheDocument();
    const header = createWrapper(container).findHeader();
    expect(header!.findHeadingText().getElement()).toHaveTextContent('Auto-Labeling');
  });

  it('shows a blank field when nothing is saved, and the constraint text names the default 5', async () => {
    setAuthRole('PortalAdmin');
    getAutoLabelingConfiguration.mockResolvedValue({ images_per_run: null, ...BOUNDS });
    await renderLoaded(null);

    expect(field()).toHaveAttribute('placeholder', '5');
    expect(screen.getByText(/^From 1 to 5\..*Leave blank to use the default, 5\.$/)).toBeInTheDocument();
  });

  it.each(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'DataLabeler'])(
    'shows an access notice instead of the form for %s and loads nothing',
    (role) => {
      setAuthRole(role);
      render(<AutoLabelingSettings />);

      expect(screen.getByText('Portal Admin access required')).toBeInTheDocument();
      expect(screen.queryByLabelText('Images per worker run')).toBeNull();
      expect(getAutoLabelingConfiguration).not.toHaveBeenCalled();
      expect(updateAutoLabelingConfiguration).not.toHaveBeenCalled();
    },
  );

  it.each(['0', '6', '2.5', '-1'])('rejects %s without calling the API', async (value) => {
    setAuthRole('PortalAdmin');
    await renderLoaded(3);

    fireEvent.change(field(), { target: { value } });
    save();

    expect(await screen.findByText(RANGE_ERROR)).toBeInTheDocument();
    expect(updateAutoLabelingConfiguration).not.toHaveBeenCalled();
  });

  it('saves 2 and shows the success alert and the returned value', async () => {
    setAuthRole('PortalAdmin');
    await renderLoaded(3);

    fireEvent.change(field(), { target: { value: '2' } });
    save();

    expect(await screen.findByText('Auto-labeling configuration saved')).toBeInTheDocument();
    expect(updateAutoLabelingConfiguration).toHaveBeenCalledTimes(1);
    expect(updateAutoLabelingConfiguration).toHaveBeenCalledWith({ images_per_run: 2 });
    expect(field()).toHaveValue(2);
    expect(screen.queryByText(RANGE_ERROR)).toBeNull();
  });

  it('saves a blank field as null, so the default applies', async () => {
    setAuthRole('PortalAdmin');
    await renderLoaded(3);

    fireEvent.change(field(), { target: { value: '' } });
    save();

    expect(await screen.findByText('Auto-labeling configuration saved')).toBeInTheDocument();
    expect(updateAutoLabelingConfiguration).toHaveBeenCalledWith({ images_per_run: null });
    expect(field()).toHaveValue(null);
  });

  it('shows the load error when the configuration fails to load', async () => {
    setAuthRole('PortalAdmin');
    getAutoLabelingConfiguration.mockRejectedValue(new Error(''));
    render(<AutoLabelingSettings />);

    expect(await screen.findByText('Failed to load auto-labeling configuration')).toBeInTheDocument();
  });

  it('shows the save error and keeps the typed value when the save fails', async () => {
    setAuthRole('PortalAdmin');
    updateAutoLabelingConfiguration.mockRejectedValue(new Error(''));
    await renderLoaded(3);

    fireEvent.change(field(), { target: { value: '4' } });
    save();

    expect(await screen.findByText('Failed to save auto-labeling configuration')).toBeInTheDocument();
    expect(updateAutoLabelingConfiguration).toHaveBeenCalledWith({ images_per_run: 4 });
    expect(field()).toHaveValue(4);
    expect(screen.queryByText('Auto-labeling configuration saved')).toBeNull();
  });
});
