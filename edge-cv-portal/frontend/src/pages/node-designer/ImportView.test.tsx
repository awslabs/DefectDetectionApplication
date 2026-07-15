/**
 * Component tests for the plugin import views (custom-node-designer
 * task 12.7, Requirements 6.3, 15.1, 15.2, 15.7): classification risk
 * badges beside Module_Listing entries (15.1), the classification and
 * its plain-language explanation displayed before the import proceeds
 * (15.2), required acknowledgment for bad/ugly/unclassified imports
 * (15.7), and the listing-failure fallback to manual repository URL
 * entry (6.3).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import ImportView from './ImportView';
import { ApiError } from '../../services/api';
import {
  CLASSIFICATION_EXPLANATIONS,
  GSTREAMER_DOCS_URL,
  GSTREAMER_PLUGIN_SETS_DOCS_URL,
  pluginDocsUrl,
} from './importFlow';

const {
  navigateMock,
  listUseCases,
  listPluginModules,
  listModulePlugins,
  importPlugin,
  selectImportPlugins,
  getVersion,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  listUseCases: vi.fn(),
  listPluginModules: vi.fn(),
  listModulePlugins: vi.fn(),
  importPlugin: vi.fn(),
  selectImportPlugins: vi.fn(),
  getVersion: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

vi.mock('../../services/api', () => {
  class ApiError extends Error {
    constructor(
      message: string,
      public readonly status?: number,
      public readonly code?: string,
      public readonly details?: Record<string, unknown>
    ) {
      super(message);
      this.name = 'ApiError';
    }
  }
  return { ApiError, apiService: { listUseCases } };
});

vi.mock('../../contexts/UsecaseContext', () => ({
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    listPluginModules,
    listModulePlugins,
    importPlugin,
    selectImportPlugins,
    getVersion,
  },
}));

const MODULES = [
  {
    name: 'gst-plugins-good',
    description: 'Well-maintained plugins',
    repoUrl: 'https://gitlab.freedesktop.org/gstreamer/gst-plugins-good.git',
    classification: 'good',
  },
  {
    name: 'gst-plugins-bad',
    description: 'Plugins lacking upstream review',
    repoUrl: 'https://gitlab.freedesktop.org/gstreamer/gst-plugins-bad.git',
    classification: 'bad',
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  listUseCases.mockResolvedValue({
    usecases: [{ usecase_id: 'uc-1', name: 'Line A' }],
  });
  listPluginModules.mockResolvedValue({ modules: MODULES, fetchedAt: 1, cached: false });
});

/** Select a module and one Target_Architecture on the form view. */
async function fillForm(container: HTMLElement, moduleName: string) {
  await waitFor(() => expect(listPluginModules).toHaveBeenCalled());
  const wrapper = createWrapper(container);

  // Selects: [0] use case, [1] module.
  const moduleSelect = wrapper.findAllSelects()[1];
  moduleSelect.openDropdown();
  moduleSelect.selectOptionByValue(moduleName);

  const archSelect = wrapper.findMultiselect()!;
  archSelect.openDropdown();
  archSelect.selectOptionByValue('x86_64');
}

describe('ImportView', () => {
  it('shows the classification badge beside the selected module (15.1)', async () => {
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-bad');

    // Badge plus the module's published repository location.
    expect(screen.getAllByText('bad').length).toBeGreaterThan(0);
    expect(
      screen.getByText('https://gitlab.freedesktop.org/gstreamer/gst-plugins-bad.git')
    ).toBeInTheDocument();
  });

  it('displays the classification with its explanation and requires acknowledgment for bad imports (15.2, 15.7)', async () => {
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-bad');
    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));

    // Confirmation view: classification and its verbatim plain-language
    // explanation shown before the import proceeds (15.2).
    await screen.findByText('Upstream classification');
    expect(screen.getByText(CLASSIFICATION_EXPLANATIONS.bad)).toBeInTheDocument();

    // Required acknowledgment gates the import for bad imports (15.7).
    const importButton = screen.getByRole('button', { name: 'Import plugin' });
    expect(importButton).toBeDisabled();
    const checkbox = createWrapper(container).findCheckbox()!;
    fireEvent.click(checkbox.findNativeInput().getElement());
    expect(screen.getByRole('button', { name: 'Import plugin' })).toBeEnabled();
  });

  it('lets good imports proceed without acknowledgment (15.7)', async () => {
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');
    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));

    await screen.findByText('Upstream classification');
    expect(screen.getByText(CLASSIFICATION_EXPLANATIONS.good)).toBeInTheDocument();
    // No acknowledgment checkbox; the import button is enabled directly.
    expect(createWrapper(container).findCheckbox()).toBeNull();
    expect(screen.getByRole('button', { name: 'Import plugin' })).toBeEnabled();
  });

  it('surfaces a listing failure and falls back to manual repository URL entry (6.3)', async () => {
    listPluginModules.mockRejectedValue(
      new ApiError(
        'The GStreamer module listing could not be fetched',
        502,
        'MODULE_LISTING_UNAVAILABLE'
      )
    );
    const { container } = render(<ImportView />);

    // The failure is surfaced with the distinct message.
    await screen.findByText('Module listing unavailable');
    expect(
      screen.getByText(/The GStreamer module listing could not be fetched/)
    ).toBeInTheDocument();

    // Manual repository URL entry is the active fallback path.
    expect(
      container.querySelector('input[placeholder="https://example.com/my-gst-plugin.git"]')
    ).not.toBeNull();
  });
});

// ------------------------------------------------ plugin-set selection

const PLUGINS_FOUND = [
  { name: 'jpeg', path: 'ext/jpeg', description: 'JPeg plugin library' },
  { name: 'rtp', path: 'gst/rtp' },
  { name: 'udp', path: 'gst/udp' },
];

// Settled record delivered in the import response (the component acts
// on any settled status directly; a 'fetching' response is polled).
const PENDING_SELECTION_RESPONSE = {
  plugin: {
    plugin_id: 'plugin-1',
    version: 1,
    import_status: 'pending_selection',
    plugins_found: PLUGINS_FOUND,
  },
  import: { status: 'pending_selection' },
};

/** Walk to the confirmation view and start the (good) import. */
async function startImportFlow(container: HTMLElement) {
  await fillForm(container, 'gst-plugins-good');
  fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
  await screen.findByText('Upstream classification');
  fireEvent.click(screen.getByRole('button', { name: 'Import plugin' }));
}

describe('ImportView plugin-set selection', () => {
  it('completes a plugin-set import only for the selected subset', async () => {
    importPlugin.mockResolvedValue(PENDING_SELECTION_RESPONSE);
    selectImportPlugins.mockResolvedValue({
      plugin: { plugin_id: 'plugin-1', version: 1, import_status: 'imported' },
      import: {
        status: 'imported',
        selected_plugins: ['rtp', 'udp'],
        submitted_architectures: ['x86_64'],
      },
    });
    const { container } = render(<ImportView />);
    await startImportFlow(container);

    // The selection dialog lists the enumerated plugins with the brief
    // description inline to the right of the name (" — description")
    // and the source path as the secondary line below.
    await screen.findByText('Select plugins to import');
    expect(screen.getByText('jpeg')).toBeInTheDocument();
    expect(screen.getByText(/JPeg plugin library/)).toBeInTheDocument();
    expect(screen.getByText('ext/jpeg')).toBeInTheDocument();
    expect(screen.getByText('gst/rtp')).toBeInTheDocument();
    expect(screen.getByText('gst/udp')).toBeInTheDocument();

    // Confirmation is gated on a non-empty selection.
    expect(
      screen.getByRole('button', { name: 'Import selected plugins' })
    ).toBeDisabled();

    // Check rtp and udp (checkbox order follows the enumeration).
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(3);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(checkboxes[2]);
    expect(screen.getByText('2 of 3 selected')).toBeInTheDocument();

    const confirm = screen.getByRole('button', { name: 'Import selected plugins' });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    // The chosen subset is recorded and the user lands on the record.
    await waitFor(() =>
      expect(selectImportPlugins).toHaveBeenCalledWith('plugin-1', 1, ['rtp', 'udp'])
    );
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith('/node-designer/plugins/plugin-1')
    );
  });

  it('supports select all, select none, and the filter box', async () => {
    importPlugin.mockResolvedValue(PENDING_SELECTION_RESPONSE);
    const { container } = render(<ImportView />);
    await startImportFlow(container);
    await screen.findByText('Select plugins to import');

    fireEvent.click(screen.getByRole('button', { name: 'Select all' }));
    expect(screen.getByText('3 of 3 selected')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Select none' }));
    expect(screen.getByText('0 of 3 selected')).toBeInTheDocument();

    // The filter box narrows the visible entries...
    const filter = screen.getByPlaceholderText('Find plugins');
    fireEvent.change(filter, { target: { value: 'rtp' } });
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
    expect(screen.queryByText('jpeg')).not.toBeInTheDocument();

    // ...and "Select all" selects only the visible plugins.
    fireEvent.click(screen.getByRole('button', { name: 'Select all' }));
    expect(screen.getByText('1 of 3 selected')).toBeInTheDocument();

    // The filter also matches per-plugin descriptions.
    fireEvent.change(filter, { target: { value: 'library' } });
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
    expect(screen.getByText('jpeg')).toBeInTheDocument();
  });

  it('skips the selection dialog for single-plugin imports', async () => {
    importPlugin.mockResolvedValue({
      plugin: {
        plugin_id: 'plugin-2',
        version: 1,
        import_status: 'imported',
        plugins_found: [{ name: 'gst-myfilter', path: '' }],
      },
      import: { status: 'imported' },
    });
    const { container } = render(<ImportView />);
    await startImportFlow(container);

    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith('/node-designer/plugins/plugin-2')
    );
    expect(screen.queryByText('Select plugins to import')).not.toBeInTheDocument();
    expect(selectImportPlugins).not.toHaveBeenCalled();
  });
});

// ---------------------------------- import-time module plugin selection

const MODULE_PLUGINS_RESPONSE = {
  module: 'gst-plugins-good',
  plugins: [{ name: 'jpeg' }, { name: 'rtp' }, { name: 'udp' }],
  fetchedAt: 1,
  cached: false,
};

const IMPORTED_RESPONSE = {
  plugin: { plugin_id: 'plugin-3', version: 1, import_status: 'imported' },
  import: { status: 'imported' },
};

describe('ImportView module plugin selection', () => {
  it('loads the plugin list defaulting to all and sends a partial selection', async () => {
    listModulePlugins.mockResolvedValue(MODULE_PLUGINS_RESPONSE);
    importPlugin.mockResolvedValue(IMPORTED_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');

    // The chosen module's plugins load with everything selected.
    await screen.findByText('Plugins to import');
    expect(listModulePlugins).toHaveBeenCalledWith('gst-plugins-good');
    expect(screen.getByText('3 of 3 selected')).toBeInTheDocument();

    // Deselect jpeg (via its checkbox: the name itself now links to
    // the plugin's docs page), review: the confirm step summarizes
    // the subset.
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    expect(screen.getByText('2 of 3 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
    await screen.findByText('Upstream classification');
    expect(screen.getByText('2 of 3 plugins: rtp, udp')).toBeInTheDocument();

    // The import carries only the selected plugins.
    fireEvent.click(screen.getByRole('button', { name: 'Import plugin' }));
    await waitFor(() => expect(importPlugin).toHaveBeenCalled());
    expect(importPlugin.mock.calls[0][0].selected_plugins).toEqual(['rtp', 'udp']);
    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith('/node-designer/plugins/plugin-3')
    );
  });

  it('keeps a full selection as a whole-module import (no selected_plugins)', async () => {
    listModulePlugins.mockResolvedValue(MODULE_PLUGINS_RESPONSE);
    importPlugin.mockResolvedValue(IMPORTED_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');
    await screen.findByText('3 of 3 selected');

    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
    await screen.findByText('Upstream classification');
    expect(screen.getByText('All plugins')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Import plugin' }));
    await waitFor(() => expect(importPlugin).toHaveBeenCalled());
    expect(importPlugin.mock.calls[0][0].selected_plugins).toBeUndefined();
  });

  it('blocks the review only while the selection is empty', async () => {
    listModulePlugins.mockResolvedValue(MODULE_PLUGINS_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');
    await screen.findByText('3 of 3 selected');

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(screen.getByText('Select at least one plugin to import')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Review import' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Select all' }));
    expect(screen.getByRole('button', { name: 'Review import' })).toBeEnabled();
  });

  it('treats a plugin-list failure as non-blocking and imports the full set', async () => {
    listModulePlugins.mockRejectedValue(
      new ApiError(
        "The plugin list for module 'gst-plugins-good' could not be retrieved",
        502,
        'MODULE_LISTING_UNAVAILABLE'
      )
    );
    importPlugin.mockResolvedValue(IMPORTED_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');

    // Non-blocking warning; the import stays available.
    await screen.findByText('Plugin list unavailable');
    expect(screen.getByRole('button', { name: 'Review import' })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
    await screen.findByText('Upstream classification');
    fireEvent.click(screen.getByRole('button', { name: 'Import plugin' }));
    await waitFor(() => expect(importPlugin).toHaveBeenCalled());
    expect(importPlugin.mock.calls[0][0].selected_plugins).toBeUndefined();
  });
});

// ------------------------------------------- asynchronous fetch flow

const FETCHING_RESPONSE = (pluginId: string) => ({
  plugin: { plugin_id: pluginId, version: 1, import_status: 'fetching' },
  import: { status: 'fetching', buildId: 'dda-plugin-fetch:abc' },
});

describe('ImportView asynchronous fetch', () => {
  // Real timers: the 3 s poll interval runs for real, so these tests
  // allow generous waitFor/test timeouts.

  it(
    'shows the cloning progress and polls until the record lands imported',
    async () => {
      importPlugin.mockResolvedValue(FETCHING_RESPONSE('plugin-9'));
      getVersion
        .mockResolvedValueOnce({
          plugin: { plugin_id: 'plugin-9', version: 1, import_status: 'fetching' },
        })
        .mockResolvedValueOnce({
          plugin: { plugin_id: 'plugin-9', version: 1, import_status: 'imported' },
        });

      const { container } = render(<ImportView />);
      await startImportFlow(container);

      // 202 'fetching': the progress state shows while the record polls.
      await screen.findByText(/Cloning repository/);
      expect(navigateMock).not.toHaveBeenCalled();

      // First poll: still fetching; second poll: imported -> navigate.
      await waitFor(
        () =>
          expect(navigateMock).toHaveBeenCalledWith(
            '/node-designer/plugins/plugin-9'
          ),
        { timeout: 10_000 }
      );
      expect(getVersion).toHaveBeenCalledWith('plugin-9', 1);
      expect(getVersion.mock.calls.length).toBeGreaterThanOrEqual(2);
    },
    15_000
  );

  it(
    'shows the recorded finding when the fetch lands failed',
    async () => {
      importPlugin.mockResolvedValue(FETCHING_RESPONSE('plugin-8'));
      getVersion.mockResolvedValue({
        plugin: {
          plugin_id: 'plugin-8',
          version: 1,
          import_status: 'failed',
          import_finding:
            'Could not retrieve the repository: it is unreachable or the ' +
            'requested revision does not exist',
        },
      });

      const { container } = render(<ImportView />);
      await startImportFlow(container);
      await screen.findByText(/Cloning repository/);

      await waitFor(
        () =>
          expect(
            screen.getByText(/Could not retrieve the repository/)
          ).toBeInTheDocument(),
        { timeout: 10_000 }
      );
      expect(navigateMock).not.toHaveBeenCalled();
      // The progress state cleared once the fetch settled.
      expect(screen.queryByText(/Cloning repository/)).not.toBeInTheDocument();
    },
    15_000
  );

  it(
    'opens the selection dialog when the polled record pends selection',
    async () => {
      importPlugin.mockResolvedValue(FETCHING_RESPONSE('plugin-1'));
      getVersion.mockResolvedValue({
        plugin: {
          plugin_id: 'plugin-1',
          version: 1,
          import_status: 'pending_selection',
          plugins_found: PLUGINS_FOUND,
        },
      });

      const { container } = render(<ImportView />);
      await startImportFlow(container);
      await screen.findByText(/Cloning repository/);

      await waitFor(
        () =>
          expect(
            screen.getByText('Select plugins to import')
          ).toBeInTheDocument(),
        { timeout: 10_000 }
      );
      expect(navigateMock).not.toHaveBeenCalled();
    },
    15_000
  );
});

// ------------------------------------------ external documentation links

describe('ImportView documentation links', () => {
  it('links the chosen module to the GStreamer documentation index', async () => {
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');

    const link = screen.getByRole('link', { name: /GStreamer documentation/ });
    expect(link).toHaveAttribute('href', GSTREAMER_DOCS_URL);
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('links the classification explanation to the plugin-set docs', async () => {
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');
    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
    await screen.findByText('Upstream classification');

    const link = screen.getByRole('link', {
      name: /Learn more about GStreamer plugin sets/,
    });
    expect(link).toHaveAttribute('href', GSTREAMER_PLUGIN_SETS_DOCS_URL);
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('links each module plugin name to its official docs page', async () => {
    listModulePlugins.mockResolvedValue(MODULE_PLUGINS_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');
    await screen.findByText('Plugins to import');

    const jpegLink = screen.getByRole('link', { name: /jpeg/ });
    expect(jpegLink).toHaveAttribute('href', pluginDocsUrl('jpeg'));
    expect(jpegLink).toHaveAttribute('target', '_blank');
  });

  it('links each enumerated plugin in the selection dialog to its docs page', async () => {
    importPlugin.mockResolvedValue(PENDING_SELECTION_RESPONSE);
    const { container } = render(<ImportView />);
    await startImportFlow(container);
    await screen.findByText('Select plugins to import');

    const rtpLink = screen.getByRole('link', { name: /rtp/ });
    expect(rtpLink).toHaveAttribute(
      'href',
      'https://gstreamer.freedesktop.org/documentation/rtp/index.html'
    );
    expect(rtpLink).toHaveAttribute('target', '_blank');
  });
});

// -------------------------------------- per-architecture revisions

describe('ImportView per-architecture revisions', () => {
  it('offers one revision input per selected architecture and sends only non-empty overrides', async () => {
    listModulePlugins.mockResolvedValue(MODULE_PLUGINS_RESPONSE);
    importPlugin.mockResolvedValue(IMPORTED_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');

    // Add a second target architecture (the multiselect dropdown is
    // still open from fillForm).
    const wrapper = createWrapper(container);
    const archSelect = wrapper.findMultiselect()!;
    archSelect.selectOptionByValue('arm64_jp5');

    // The optional expandable section lists one input per selected
    // architecture, blank inputs following the top-level revision.
    fireEvent.click(screen.getByText('Per-architecture revisions'));
    expect(screen.getByLabelText('Revision for x86_64')).toBeInTheDocument();
    const jp5Input = screen.getByLabelText('Revision for arm64 JetPack 5');
    fireEvent.change(jp5Input, { target: { value: '1.16' } });

    // The confirm step shows every selected architecture's effective
    // revision when overrides are present.
    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
    await screen.findByText('Upstream classification');
    expect(screen.getByText('Per-architecture revisions')).toBeInTheDocument();
    expect(screen.getByText('arm64 JetPack 5: 1.16')).toBeInTheDocument();
    expect(screen.getByText('x86_64: default branch')).toBeInTheDocument();

    // Only the non-empty override rides the import request.
    fireEvent.click(screen.getByRole('button', { name: 'Import plugin' }));
    await waitFor(() => expect(importPlugin).toHaveBeenCalled());
    expect(importPlugin.mock.calls[0][0].arch_revisions).toEqual({
      arm64_jp5: '1.16',
    });
    expect(importPlugin.mock.calls[0][0].architectures).toEqual(
      expect.arrayContaining(['x86_64', 'arm64_jp5'])
    );
  });

  it('omits arch_revisions entirely when every override is blank', async () => {
    listModulePlugins.mockResolvedValue(MODULE_PLUGINS_RESPONSE);
    importPlugin.mockResolvedValue(IMPORTED_RESPONSE);
    const { container } = render(<ImportView />);
    await fillForm(container, 'gst-plugins-good');

    fireEvent.click(screen.getByText('Per-architecture revisions'));
    fireEvent.change(screen.getByLabelText('Revision for x86_64'), {
      target: { value: '   ' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review import' }));
    await screen.findByText('Upstream classification');
    // No per-arch section on the confirm step without overrides.
    expect(screen.queryByText('Per-architecture revisions')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Import plugin' }));
    await waitFor(() => expect(importPlugin).toHaveBeenCalled());
    expect(importPlugin.mock.calls[0][0].arch_revisions).toBeUndefined();
  });
});
