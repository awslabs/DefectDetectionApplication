/**
 * Component tests for the Plugin_Record detail page's import-selection
 * display: a partial plugin-set import shows which plugins it covers
 * ("Imported plugins" in the overview), a settled import without a
 * recorded selection shows the whole enumeration, and non-imports show
 * no such field.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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

describe('PluginDetail revision adjustment (bug condition exploration)', () => {
  // Bug condition exploration for the imported-plugin revision
  // adjustment dead end (Property 1). EXPECTED TO FAIL on unfixed
  // code: the UI renders the incompatible-platform advice as plain
  // text with no control to apply the suggested revision (bug 1.1).
  // Once the fix lands, this same test validates the expected
  // behavior: an adjust-revision action pre-filled with the recorded
  // suggestedRevision, editable by the user.
  //
  // **Validates: Requirements 1.1, 1.3**
  const jp4IncompatibleMap = {
    arm64_jp4: {
      compatible: false,
      platformVersion: '1.14',
      requiredVersion: '1.24.0',
      reason:
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 provides 1.14',
      suggestedRevision: '1.14',
    },
  };

  it('offers an adjust-revision control pre-filled with the suggested revision', async () => {
    await renderDetail(
      importedDetail({
        artifacts: {
          arm64_jp4: { buildStatus: 'failed', logTail: 'meson: error' },
        },
        platform_compatibility: jp4IncompatibleMap,
      })
    );

    // The advisory warning renders (unchanged behavior)...
    expect(
      screen.getByText(
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 ' +
          'provides 1.14. Import revision 1.14 for this platform instead.'
      )
    ).toBeInTheDocument();

    // ...and an action exists to act on it (expected behavior; the
    // unfixed page renders the advice as text only).
    const adjust = screen.getByRole('button', { name: /adjust revision/i });
    fireEvent.click(adjust);

    // The revision input is pre-filled with the recorded suggestion.
    expect(screen.getByDisplayValue('1.14')).toBeInTheDocument();
  });
});

describe('PluginDetail revision adjustment (fix specifics)', () => {
  // Unit tests for the adjust-revision action beyond the exploration
  // coverage above: the action renders EXACTLY for incompatible
  // entries carrying a suggestion on settled imports, the pre-filled
  // input is editable, Apply calls the API and refreshes the page
  // state, and errors surface on the affected platform's entry only.
  //
  // **Validates: Requirements 2.1, 2.4**
  const jp4Incompatible = {
    compatible: false,
    platformVersion: '1.14',
    requiredVersion: '1.24.0',
    reason:
      'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 provides 1.14',
    suggestedRevision: '1.14',
  };

  function detailWithWarning(
    overrides: Partial<PluginVersionDetail> = {}
  ): PluginVersionDetail {
    return importedDetail({
      artifacts: {
        arm64_jp4: { buildStatus: 'failed', logTail: 'meson: error' },
      },
      platform_compatibility: { arm64_jp4: jp4Incompatible },
      ...overrides,
    });
  }

  async function mockAdjustRevision(mock: ReturnType<typeof vi.fn>) {
    const api = (await import('./api')).nodeDesignerApi as Record<
      string,
      unknown
    >;
    api.adjustRevision = mock;
  }

  it('offers no action for incompatible entries without a suggested revision', async () => {
    await renderDetail(
      detailWithWarning({
        platform_compatibility: {
          arm64_jp4: { ...jp4Incompatible, suggestedRevision: null },
        },
      })
    );

    // The advisory warning still renders, but without the action.
    expect(screen.getByText(/requires GStreamer/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /adjust revision/i })
    ).not.toBeInTheDocument();
  });

  it('offers no action while the import has not settled', async () => {
    await renderDetail(detailWithWarning({ import_status: 'failed' }));

    expect(
      screen.queryByRole('button', { name: /adjust revision/i })
    ).not.toBeInTheDocument();
  });

  it('applies the edited revision via the API and refreshes the page state', async () => {
    const adjustedView = {
      plugin_id: 'p-1',
      version: 1,
      requested_architectures: ['arm64_jp4', 'x86_64'],
      builds: {
        arm64_jp4: { buildStatus: 'queued', logTail: '', prebuilt: false },
        x86_64: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
      },
      settled: false,
      component_packaging_triggered: false,
    };
    const adjustRevision = vi.fn().mockResolvedValue({
      plugin: detailWithWarning(),
      builds: adjustedView,
    });
    await mockAdjustRevision(adjustRevision);
    await renderDetail(detailWithWarning());

    fireEvent.click(screen.getByRole('button', { name: /adjust revision/i }));

    // The pre-filled suggestion is editable before applying.
    const input = screen.getByDisplayValue('1.14');
    fireEvent.change(input, { target: { value: '1.16' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(adjustRevision).toHaveBeenCalledWith('p-1', 1, 'arm64_jp4', '1.16')
    );
    // The page reflects the response's builds view (the platform is
    // queued while the adjustment fetch runs) and the input closes.
    await waitFor(() =>
      expect(screen.getByText('arm64 JetPack 4: queued')).toBeInTheDocument()
    );
    expect(screen.queryByDisplayValue('1.16')).not.toBeInTheDocument();
  });

  it('surfaces the adjustment error on the affected platform only', async () => {
    const adjustRevision = vi
      .fn()
      .mockRejectedValue(new Error('The revision 9.99 does not exist'));
    await mockAdjustRevision(adjustRevision);
    await renderDetail(
      detailWithWarning({
        artifacts: {
          arm64_jp4: { buildStatus: 'failed', logTail: 'meson: error' },
          arm64_jp5: { buildStatus: 'failed', logTail: 'meson: error' },
        },
        platform_compatibility: {
          arm64_jp4: jp4Incompatible,
          arm64_jp5: {
            ...jp4Incompatible,
            platformVersion: '1.16',
            reason:
              'The source requires GStreamer >= 1.24.0; arm64 JetPack 5 provides 1.16',
            suggestedRevision: '1.16',
          },
        },
      })
    );

    // Both incompatible platforms carry their own action.
    const actions = screen.getAllByRole('button', {
      name: /adjust revision/i,
    });
    expect(actions).toHaveLength(2);

    fireEvent.click(actions[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    // The rejection surfaces once, on the platform that was adjusted.
    await waitFor(() =>
      expect(
        screen.getAllByText('The revision 9.99 does not exist')
      ).toHaveLength(1)
    );
    // The action is still available to retry the adjustment.
    expect(
      screen.getAllByRole('button', { name: /adjust revision/i }).length
    ).toBeGreaterThanOrEqual(1);
  });
});

describe('PluginDetail revision adjustment (end-to-end detail-page flow)', () => {
  // Integration flow (task 5): the incompatible-platform warning
  // renders with the adjust action -> Apply calls the endpoint -> the
  // page state reflects the response's builds view and the revision
  // label shows the adjusted revision once arm64_jp4 maps through
  // arch_revisions -> fetches -> the build poll resumes because the
  // view is no longer settled.
  //
  // **Validates: Requirements 2.4, 3.3**
  const jp4Incompatible = {
    compatible: false,
    platformVersion: '1.14',
    requiredVersion: '1.24.0',
    reason:
      'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 provides 1.14',
    suggestedRevision: '1.14',
  };

  const settledBuilds = {
    plugin_id: 'p-1',
    version: 1,
    requested_architectures: ['arm64_jp4', 'x86_64'],
    builds: {
      arm64_jp4: { buildStatus: 'failed', logTail: 'meson: error', prebuilt: false },
      x86_64: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
    },
    settled: true,
    component_packaging_triggered: true,
  };

  afterEach(() => {
    vi.useRealTimers();
  });

  it('applies the adjustment, maps the revision label, and resumes the poll', async () => {
    // shouldAdvanceTime keeps waitFor and promise resolution working
    // in real time while letting the test jump the 10 s poll interval.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const detail = importedDetail({
      artifacts: {
        arm64_jp4: { buildStatus: 'failed', logTail: 'meson: error' },
        x86_64: { buildStatus: 'succeeded' },
      },
      platform_compatibility: { arm64_jp4: jp4Incompatible },
    });
    getBuilds.mockResolvedValue(settledBuilds);
    // The endpoint's 202 response: the '1.14' tree is recorded in the
    // fetches map, arm64_jp4 mapped through arch_revisions, its build
    // queued — the view is no longer settled.
    const adjustedPlugin = {
      ...detail,
      fetches: {
        '1.14': {
          revision: '1.14',
          source_prefix: 'plugin-sources/uc-1/p-1/1/rev-1.14/',
          status: 'succeeded',
        },
      },
      arch_revisions: { arm64_jp4: '1.14' },
    };
    const queuedView = {
      ...settledBuilds,
      builds: {
        ...settledBuilds.builds,
        arm64_jp4: { buildStatus: 'queued', logTail: '', prebuilt: false },
      },
      settled: false,
    };
    const adjustRevision = vi
      .fn()
      .mockResolvedValue({ plugin: adjustedPlugin, builds: queuedView });
    const api = (await import('./api')).nodeDesignerApi as Record<string, unknown>;
    api.adjustRevision = adjustRevision;

    await renderDetail(detail);

    // The warning renders with the action.
    expect(
      screen.getByText(
        'The source requires GStreamer >= 1.24.0; arm64 JetPack 4 ' +
          'provides 1.14. Import revision 1.14 for this platform instead.'
      )
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /adjust revision/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(adjustRevision).toHaveBeenCalledWith('p-1', 1, 'arm64_jp4', '1.14')
    );
    // The page reflects the response's builds view...
    await waitFor(() =>
      expect(screen.getByText('arm64 JetPack 4: queued')).toBeInTheDocument()
    );
    // ...and the revision label shows the adjusted revision once the
    // architecture is mapped.
    expect(screen.getByText('revision 1.14')).toBeInTheDocument();

    // The poll resumes (the view is no longer settled): the next tick
    // refreshes the builds view and the page shows the running build.
    const pollsBefore = getBuilds.mock.calls.length;
    getBuilds.mockResolvedValue({
      ...queuedView,
      builds: {
        ...queuedView.builds,
        arm64_jp4: { buildStatus: 'building', logTail: '', prebuilt: false },
      },
    });
    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() =>
      expect(getBuilds.mock.calls.length).toBeGreaterThan(pollsBefore)
    );
    await waitFor(() =>
      expect(screen.getByText('arm64 JetPack 4: building')).toBeInTheDocument()
    );
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
