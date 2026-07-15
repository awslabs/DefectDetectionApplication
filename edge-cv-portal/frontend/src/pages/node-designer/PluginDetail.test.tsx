/**
 * Component tests for the Plugin_Record detail page's import-selection
 * display: a partial plugin-set import shows which plugins it covers
 * ("Imported plugins" in the overview), a settled import without a
 * recorded selection shows the whole enumeration, and non-imports show
 * no such field.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import PluginDetail from './PluginDetail';
import type { PluginVersionDetail } from './types';

const {
  navigateMock,
  getPlugin,
  getBuilds,
  getVersionSource,
  deletePlugin,
  listNodeTypes,
  promoteVersion,
  demoteVersion,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  getPlugin: vi.fn(),
  getBuilds: vi.fn(),
  getVersionSource: vi.fn(),
  deletePlugin: vi.fn(),
  listNodeTypes: vi.fn(),
  promoteVersion: vi.fn(),
  demoteVersion: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ pluginId: 'p-1' }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    getPlugin,
    getBuilds,
    getVersionSource,
    deletePlugin,
    listNodeTypes,
    promoteVersion,
    demoteVersion,
  },
}));

function importedDetail(
  overrides: Partial<PluginVersionDetail> = {}
): PluginVersionDetail {
  return {
    plugin_id: 'p-1',
    version: 1,
    usecase_id: 'uc-1',
    name: 'gst-plugins-good-rtsp',
    description: '',
    kind: 'imported',
    deepstream: false,
    provenance: { classification: 'good' },
    lifecycle_state: 'dev',
    review: { decision: 'pending' },
    artifacts: {},
    component: {},
    source_s3_prefix: 'plugin-sources/uc-1/p-1/1/',
    created_by: 'user-1',
    created_at: 1,
    updated_at: 1,
    import_status: 'imported',
    plugins_found: [
      { name: 'jpeg', path: 'ext/jpeg' },
      { name: 'rtsp', path: 'gst/rtsp' },
      { name: 'udp', path: 'gst/udp' },
    ],
    selected_plugins: ['rtsp'],
    ...overrides,
  };
}

async function renderDetail(plugin: PluginVersionDetail) {
  getPlugin.mockResolvedValue({ plugin, versions: [] });
  render(<PluginDetail />);
  await waitFor(() => expect(screen.getByText(plugin.name)).toBeInTheDocument());
}

beforeEach(() => {
  vi.clearAllMocks();
  getBuilds.mockResolvedValue(null);
  getVersionSource.mockResolvedValue({ files: [] });
  listNodeTypes.mockResolvedValue({ nodeTypes: [], count: 0 });
});

describe('PluginDetail import selection display', () => {
  it('shows a partial selection with counts in the overview', async () => {
    await renderDetail(importedDetail());

    expect(screen.getByText('Imported plugins')).toBeInTheDocument();
    expect(screen.getByText('rtsp (1 of 3 found)')).toBeInTheDocument();
  });

  it('shows the whole enumeration when no selection was recorded', async () => {
    await renderDetail(
      importedDetail({
        name: 'gst-plugins-good',
        selected_plugins: undefined,
      })
    );

    expect(screen.getByText('Imported plugins')).toBeInTheDocument();
    expect(screen.getByText('All 3 plugins')).toBeInTheDocument();
  });

  it('shows no imported-plugins field for non-imports', async () => {
    await renderDetail(
      importedDetail({
        name: 'my-scaffold',
        kind: 'scaffold',
        import_status: undefined,
        plugins_found: undefined,
        selected_plugins: undefined,
      })
    );

    expect(screen.queryByText('Imported plugins')).not.toBeInTheDocument();
  });
});

describe('PluginDetail build retry', () => {
  const failedBuilds = {
    plugin_id: 'p-1',
    version: 1,
    requested_architectures: ['arm64_jp5', 'x86_64'],
    builds: {
      x86_64: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
      arm64_jp5: { buildStatus: 'failed', logTail: 'meson: error', prebuilt: false },
      arm64_jp4: { buildStatus: 'failed', logTail: 'meson: error', prebuilt: false },
    },
    settled: true,
    component_packaging_triggered: false,
  };

  it('offers Retry build on failed architectures and re-submits them', async () => {
    const { fireEvent } = await import('@testing-library/react');
    const startBuilds = vi.fn().mockResolvedValue({
      ...failedBuilds,
      builds: {
        ...failedBuilds.builds,
        arm64_jp5: { buildStatus: 'building', logTail: '', prebuilt: false },
      },
    });
    const api = (await import('./api')).nodeDesignerApi as Record<string, unknown>;
    api.startBuilds = startBuilds;
    getBuilds.mockResolvedValue(failedBuilds);

    await renderDetail(importedDetail());

    // One retry button per failed arch, none on the succeeded one.
    const retryButtons = await screen.findAllByRole('button', { name: 'Retry build' });
    expect(retryButtons).toHaveLength(2);

    // A retry-all action appears when more than one build failed.
    expect(
      screen.getByRole('button', { name: 'Retry failed builds' })
    ).toBeInTheDocument();

    fireEvent.click(retryButtons[0]);
    await waitFor(() =>
      expect(startBuilds).toHaveBeenCalledWith('p-1', 1, ['arm64_jp4'])
    );
  });

  it('retries every failed architecture from the header action', async () => {
    const { fireEvent } = await import('@testing-library/react');
    const startBuilds = vi.fn().mockResolvedValue(failedBuilds);
    const api = (await import('./api')).nodeDesignerApi as Record<string, unknown>;
    api.startBuilds = startBuilds;
    getBuilds.mockResolvedValue(failedBuilds);

    await renderDetail(importedDetail());

    fireEvent.click(
      await screen.findByRole('button', { name: 'Retry failed builds' })
    );
    await waitFor(() =>
      expect(startBuilds).toHaveBeenCalledWith('p-1', 1, ['arm64_jp4', 'arm64_jp5'])
    );
  });

  it('shows the error and keeps the buttons when the retry fails', async () => {
    const { fireEvent } = await import('@testing-library/react');
    const startBuilds = vi
      .fn()
      .mockRejectedValue(new Error('build submission rejected'));
    const api = (await import('./api')).nodeDesignerApi as Record<string, unknown>;
    api.startBuilds = startBuilds;
    getBuilds.mockResolvedValue(failedBuilds);

    await renderDetail(importedDetail());

    const retryButtons = await screen.findAllByRole('button', { name: 'Retry build' });
    fireEvent.click(retryButtons[0]);

    await waitFor(() =>
      expect(screen.getByText('build submission rejected')).toBeInTheDocument()
    );
    expect(screen.getAllByRole('button', { name: 'Retry build' })).toHaveLength(2);
  });
});

describe('PluginDetail platform compatibility warnings', () => {
  const compatibilityMap = {
    x86_64: {
      compatible: true,
      platformVersion: '1.20',
      requiredVersion: '1.24.0',
      reason: null,
      suggestedRevision: null,
    },
    arm64_jp5: {
      compatible: false,
      platformVersion: '1.16',
      requiredVersion: '1.24.0',
      reason:
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 5 provides 1.16',
      suggestedRevision: '1.16',
    },
  };

  it('warns under incompatible architectures with the working revision', async () => {
    await renderDetail(
      importedDetail({
        artifacts: {
          x86_64: { buildStatus: 'succeeded' },
          arm64_jp5: { buildStatus: 'failed', logTail: 'meson: error' },
        },
        platform_compatibility: compatibilityMap,
      })
    );

    expect(
      screen.getByText(
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 5 ' +
          'provides 1.16. Import revision 1.16 for this platform instead.'
      )
    ).toBeInTheDocument();
  });

  it('shows no warning for compatible architectures or without a map', async () => {
    await renderDetail(
      importedDetail({
        artifacts: { x86_64: { buildStatus: 'succeeded' } },
        platform_compatibility: { x86_64: compatibilityMap.x86_64 },
      })
    );

    expect(screen.queryByText(/requires GStreamer/)).not.toBeInTheDocument();
  });
});

describe('PluginDetail record deletion', () => {
  const modalMessage =
    'Delete gst-plugins-good-rtsp? This removes the record, its source ' +
    'snapshot, and built artifacts. This cannot be undone.';

  async function openDeleteModal(): Promise<HTMLElement> {
    fireEvent.click(
      screen.getByRole('button', { name: 'Delete gst-plugins-good-rtsp' })
    );
    // Scope to the confirmation dialog: the modal's confirm button
    // reads "Delete" like other buttons on the page.
    const dialog = (await screen.findByText(modalMessage)).closest(
      '[class*="awsui_dialog"]'
    ) as HTMLElement;
    expect(dialog).not.toBeNull();
    return dialog;
  }

  it('opens a confirmation modal from the header Delete action', async () => {
    await renderDetail(importedDetail());

    await openDeleteModal();

    expect(screen.getByText(modalMessage)).toBeInTheDocument();
    expect(deletePlugin).not.toHaveBeenCalled();
  });

  it('cancel closes the modal without deleting anything', async () => {
    await renderDetail(importedDetail());
    const dialog = await openDeleteModal();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    // Cloudscape keeps the closed modal mounted (marked hidden), so
    // closed means the dialog carries the awsui hidden marker again.
    await waitFor(() =>
      expect(screen.getByRole('dialog').className).toContain('hidden')
    );
    expect(deletePlugin).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it('confirm deletes the record and returns to the library', async () => {
    deletePlugin.mockResolvedValue({
      deleted: true,
      plugin_id: 'p-1',
      versions: [1],
    });
    await renderDetail(importedDetail());
    const dialog = await openDeleteModal();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deletePlugin).toHaveBeenCalledWith('p-1'));
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith('/node-designer')
    );
  });

  it('shows the error message when the delete is refused', async () => {
    deletePlugin.mockRejectedValue(
      new Error(
        'This Plugin_Record has versions promoted beyond dev and cannot ' +
          'be deleted; demote them first'
      )
    );
    await renderDetail(importedDetail());
    const dialog = await openDeleteModal();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() =>
      expect(
        screen.getByText(/versions promoted beyond dev/)
      ).toBeInTheDocument()
    );
    expect(navigateMock).not.toHaveBeenCalled();
  });
});

describe('PluginDetail lifecycle transitions', () => {
  it('promotes a dev version to test', async () => {
    promoteVersion.mockResolvedValue({
      plugin: importedDetail({ lifecycle_state: 'test' }),
    });
    await renderDetail(importedDetail());

    const promote = screen.getByRole('button', { name: /promote to test/i });
    fireEvent.click(promote);

    await waitFor(() =>
      expect(promoteVersion).toHaveBeenCalledWith('p-1', 1)
    );
    // The header action flips to the next transition pair.
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /promote to prod/i })
      ).toBeInTheDocument()
    );
    expect(
      screen.getByRole('button', { name: /demote to dev/i })
    ).toBeInTheDocument();
  });

  it('offers only a demote action for prod versions', async () => {
    await renderDetail(importedDetail({ lifecycle_state: 'prod' }));

    expect(
      screen.getByRole('button', { name: /demote to test/i })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /promote/i })
    ).not.toBeInTheDocument();
  });

  it('demotes a test version back to dev', async () => {
    demoteVersion.mockResolvedValue({
      plugin: importedDetail({ lifecycle_state: 'dev' }),
    });
    await renderDetail(importedDetail({ lifecycle_state: 'test' }));

    fireEvent.click(screen.getByRole('button', { name: /demote to dev/i }));

    await waitFor(() => expect(demoteVersion).toHaveBeenCalledWith('p-1', 1));
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /promote to test/i })
      ).toBeInTheDocument()
    );
  });

  it('surfaces the 409 gate rejection for an unbuildable promotion', async () => {
    promoteVersion.mockRejectedValue(
      new Error(
        'Promotion from dev to test requires at least one successfully ' +
          'built Plugin_Artifact; none exists for this Plugin_Record version'
      )
    );
    await renderDetail(importedDetail());

    fireEvent.click(screen.getByRole('button', { name: /promote to test/i }));

    await waitFor(() =>
      expect(
        screen.getByText('Lifecycle transition rejected')
      ).toBeInTheDocument()
    );
    expect(
      screen.getByText(/requires at least one successfully built/i)
    ).toBeInTheDocument();
  });
});
