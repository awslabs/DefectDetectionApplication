/**
 * Component tests for the plugin library list's import-selection
 * display: a partial plugin-set import surfaces its selection
 * compactly under the record name ('rtsp' or '3 plugins') so a
 * single-plugin import is not mistaken for the whole library.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import PluginLibrary from './PluginLibrary';
import type { PluginRecordSummary } from './types';

const { navigateMock, listUseCases, listPlugins, deletePlugin } = vi.hoisted(
  () => ({
    navigateMock: vi.fn(),
    listUseCases: vi.fn(),
    listPlugins: vi.fn(),
    deletePlugin: vi.fn(),
  })
);

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

vi.mock('../../services/api', () => ({
  apiService: { listUseCases },
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: { listPlugins, deletePlugin },
}));

function summary(overrides: Partial<PluginRecordSummary>): PluginRecordSummary {
  return {
    plugin_id: 'p-1',
    version: 1,
    usecase_id: 'uc-1',
    name: 'gst-plugins-good',
    kind: 'imported',
    deepstream: false,
    lifecycle_state: 'dev',
    review_decision: 'pending',
    import_status: 'imported',
    build_status: {},
    updated_at: 1,
    ...overrides,
  };
}

async function renderLibrary(plugins: PluginRecordSummary[]) {
  listPlugins.mockResolvedValue({ plugins, count: plugins.length });
  render(<PluginLibrary />);
  await waitFor(() => expect(listPlugins).toHaveBeenCalled());
}

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
});

describe('PluginLibrary import selection display', () => {
  it('shows a single-plugin selection under the record name', async () => {
    await renderLibrary([
      summary({
        name: 'gst-plugins-good-rtsp',
        selected_plugins: ['rtsp'],
        plugins_found_count: 74,
      }),
    ]);

    expect(screen.getByText('gst-plugins-good-rtsp')).toBeInTheDocument();
    expect(screen.getByText('rtsp')).toBeInTheDocument();
  });

  it('shows a plugin count for larger partial selections', async () => {
    await renderLibrary([
      summary({
        selected_plugins: ['rtp', 'udp', 'jpeg'],
        plugins_found_count: 74,
      }),
    ]);

    expect(screen.getByText('3 plugins')).toBeInTheDocument();
  });

  it('stays quiet for full selections and non-imports', async () => {
    await renderLibrary([
      summary({
        plugin_id: 'p-full',
        selected_plugins: ['a', 'b'],
        plugins_found_count: 2,
      }),
      summary({
        plugin_id: 'p-scaffold',
        name: 'my-scaffold',
        kind: 'scaffold',
        import_status: undefined,
      }),
    ]);

    expect(screen.queryByText('2 plugins')).not.toBeInTheDocument();
    expect(screen.queryByText('plugins', { exact: true })).not.toBeInTheDocument();
  });
});

describe('PluginLibrary record deletion', () => {
  const modalMessage =
    'Delete gst-plugins-good? This removes the record, its source ' +
    'snapshot, and built artifacts. This cannot be undone.';

  async function openDeleteModal(): Promise<HTMLElement> {
    fireEvent.click(
      screen.getByRole('button', { name: 'Delete gst-plugins-good' })
    );
    // Scope to the confirmation dialog: the row action and the modal's
    // confirm button both read "Delete".
    const dialog = (await screen.findByText(modalMessage)).closest(
      '[class*="awsui_dialog"]'
    ) as HTMLElement;
    expect(dialog).not.toBeNull();
    return dialog;
  }

  it('opens the confirmation modal from the row Delete action', async () => {
    await renderLibrary([summary({})]);

    await openDeleteModal();

    expect(screen.getByText(modalMessage)).toBeInTheDocument();
    expect(deletePlugin).not.toHaveBeenCalled();
  });

  it('cancel closes the modal without deleting anything', async () => {
    await renderLibrary([summary({})]);
    const dialog = await openDeleteModal();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    // Cloudscape keeps the closed modal mounted (marked hidden), so
    // closed means the dialog carries the awsui hidden marker again.
    await waitFor(() =>
      expect(screen.getByRole('dialog').className).toContain('hidden')
    );
    expect(deletePlugin).not.toHaveBeenCalled();
    expect(listPlugins).toHaveBeenCalledTimes(1);
  });

  it('confirm deletes the record and refreshes the list', async () => {
    deletePlugin.mockResolvedValue({
      deleted: true,
      plugin_id: 'p-1',
      versions: [1],
    });
    await renderLibrary([summary({})]);
    listPlugins.mockResolvedValue({ plugins: [], count: 0 });
    const dialog = await openDeleteModal();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deletePlugin).toHaveBeenCalledWith('p-1'));
    // The list reloads after the delete.
    await waitFor(() => expect(listPlugins).toHaveBeenCalledTimes(2));
    expect(listPlugins).toHaveBeenLastCalledWith('uc-1');
  });

  it('shows the API error message when the delete fails', async () => {
    deletePlugin.mockRejectedValue(
      new Error(
        'This Plugin_Record has versions promoted beyond dev and cannot ' +
          'be deleted; demote them first'
      )
    );
    await renderLibrary([summary({})]);
    const dialog = await openDeleteModal();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() =>
      expect(
        screen.getByText(/versions promoted beyond dev/)
      ).toBeInTheDocument()
    );
    // The record stays listed (no refresh happened).
    expect(listPlugins).toHaveBeenCalledTimes(1);
    expect(screen.getByText('gst-plugins-good')).toBeInTheDocument();
  });
});
