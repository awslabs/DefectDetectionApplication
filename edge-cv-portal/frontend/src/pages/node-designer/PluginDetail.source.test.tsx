/**
 * Component tests for the Detail_Page's source lifecycle integration
 * (custom-node-source-lifecycle tasks 11.3, 13.3; Requirements 1.4-1.8,
 * 1.10-1.12, 5.1, 5.2, 6.1-6.3, 6.6, 6.8, 8.3, 9.2).
 *
 * Covers: the Source_Editor loaded from the bulk read with the revision in
 * the header; Save reducing the editor to the PUT body and, on success,
 * rebaselining, reporting stale architectures, and offering a Rebuild
 * (1.4, 1.5, 1.8); the SOURCE_LOCKED, SOURCE_REVISION_CONFLICT, and
 * SCAFFOLD_INVALID recovery paths (1.6, 1.10, 1.12); Save as new version
 * (1.6); the leave-page guard on unsaved edits (1.11); "Rebuild required"
 * badges and the Plugin_Component summary (1.8, 8.3); "Fix with AI"
 * seeding the assistant with the failing build's log (5.1) and a
 * simulator-routed Diagnostic_Context (5.2); Add architectures listing
 * only registry-minus-requested targets, submitting only the added ones,
 * surfacing BUILD_TARGET_UNAVAILABLE, and being blocked on prod (6.1-6.3,
 * 6.6, 6.8); and Viewer role gating (9.2).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import PluginDetail from './PluginDetail';
import { ApiError } from '../../services/api';
import type { PluginBuildsView, PluginVersionDetail, SourceFileEntry } from './types';

const {
  navigateMock,
  locationState,
  authRole,
  getPlugin,
  getBuilds,
  getSourceTree,
  saveSource,
  createNewVersion,
  startBuilds,
  addArchitectures,
  deletePlugin,
  listNodeTypes,
  listGitConnections,
  listSyncOperations,
} = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  locationState: { value: null as unknown },
  authRole: { value: 'UseCaseAdmin' as string },
  getPlugin: vi.fn(),
  getBuilds: vi.fn(),
  getSourceTree: vi.fn(),
  saveSource: vi.fn(),
  createNewVersion: vi.fn(),
  startBuilds: vi.fn(),
  addArchitectures: vi.fn(),
  deletePlugin: vi.fn(),
  listNodeTypes: vi.fn(),
  listGitConnections: vi.fn(),
  listSyncOperations: vi.fn(),
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      user_id: 'u-1',
      email: 'user@example.com',
      username: 'user',
      role: authRole.value,
      is_super_user: false,
    },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ pluginId: 'p-1' }),
  useLocation: () => ({ state: locationState.value, pathname: '/node-designer/plugins/p-1' }),
}));

vi.mock('./api', () => ({
  nodeDesignerApi: {
    getPlugin,
    getBuilds,
    getSourceTree,
    saveSource,
    createNewVersion,
    startBuilds,
    addArchitectures,
    deletePlugin,
    listNodeTypes,
    listGitConnections,
    listSyncOperations,
  },
}));

// -------------------------------------------------------------- fixtures

const MESON = "project('demo', 'c')\n";
const PLUGIN_C = '#include <gst/gst.h>\n';
const BUILD_MESON = "subdir('src')\n";

function scaffoldDetail(overrides: Partial<PluginVersionDetail> = {}): PluginVersionDetail {
  return {
    plugin_id: 'p-1',
    version: 1,
    usecase_id: 'uc-1',
    name: 'my-scaffold',
    description: '',
    kind: 'scaffold',
    deepstream: false,
    provenance: {
      classification: 'good',
      scaffoldDeclaration: JSON.stringify({
        typeId: 'resize_image',
        architectures: ['x86_64'],
        parameters: [{ name: 'threshold', paramType: 'float' }],
      }),
    },
    lifecycle_state: 'dev',
    review: { decision: 'pending' },
    artifacts: {},
    component: {},
    source_s3_prefix: 'plugin-sources/uc-1/p-1/1/',
    source_revision: 3,
    stale_architectures: [],
    created_by: 'user-1',
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

function sourceTree(): SourceFileEntry[] {
  return [
    { file: 'meson.build', size: MESON.length, content: MESON },
    { file: 'src/plugin.c', size: PLUGIN_C.length, content: PLUGIN_C },
    { file: 'builds/arm64_jp5/meson.build', size: BUILD_MESON.length, content: BUILD_MESON },
  ];
}

function buildsView(overrides: Partial<PluginBuildsView> = {}): PluginBuildsView {
  return {
    plugin_id: 'p-1',
    version: 1,
    requested_architectures: ['arm64_jp5', 'x86_64'],
    builds: {
      x86_64: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
      arm64_jp5: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
    },
    settled: true,
    component_packaging_triggered: false,
    source_revision: 3,
    stale_architectures: [],
    buildable_architectures: ['x86_64', 'arm64_jp4', 'arm64_jp5', 'arm64_jp6', 'arm64_jp7'],
    component: { version: '0.2', revision: 2, architectures: ['arm64_jp5', 'x86_64'], status: 'published' },
    ...overrides,
  };
}

// --------------------------------------------------------------- helpers

async function renderDetail(plugin: PluginVersionDetail) {
  getPlugin.mockResolvedValue({ plugin, versions: [] });
  render(<PluginDetail />);
  await waitFor(() => expect(screen.getByText(plugin.name)).toBeInTheDocument());
  // The editor has mounted once the first source tab renders.
  await screen.findByRole('tab', { name: 'meson.build' });
}

const sourceOf = (path: string) =>
  screen.getByRole('textbox', { name: `Source of ${path}` }) as HTMLTextAreaElement;

const isDisabled = (button: HTMLElement) =>
  button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

function editActiveFile(path: string, content: string) {
  fireEvent.click(screen.getByRole('tab', { name: path }));
  fireEvent.change(sourceOf(path), { target: { value: content } });
}

/** Pick an option in a Cloudscape Multiselect by its trigger label. */
async function pickOption(triggerName: string | RegExp, optionLabel: string | RegExp) {
  const trigger = screen.getByRole('button', { name: triggerName });
  fireEvent.mouseDown(trigger);
  fireEvent.click(trigger);
  const option = await screen.findByRole('option', { name: optionLabel });
  fireEvent.mouseDown(option);
  fireEvent.mouseUp(option);
  fireEvent.click(option);
}

beforeEach(() => {
  vi.clearAllMocks();
  authRole.value = 'UseCaseAdmin';
  locationState.value = null;
  getBuilds.mockResolvedValue(buildsView());
  getSourceTree.mockResolvedValue({
    source_revision: 3,
    files: sourceTree(),
    count: 3,
    truncated: false,
  });
  listGitConnections.mockResolvedValue({ connections: [], count: 0 });
  listSyncOperations.mockResolvedValue({ operations: [], count: 0 });
  listNodeTypes.mockResolvedValue({ nodeTypes: [], count: 0 });
});

// ----------------------------------------------------------------- tests

describe('PluginDetail source editor', () => {
  it('loads the source tree into the editor with the revision in the header (1.1, 1.2)', async () => {
    await renderDetail(scaffoldDetail());

    expect(getSourceTree).toHaveBeenCalledWith('p-1', 1);
    expect(
      screen.getByText('Source revision 3. Edits are saved in place on this dev version.')
    ).toBeInTheDocument();
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual([
      'builds/arm64_jp5/meson.build',
      'meson.build',
      'src/plugin.c',
    ]);
    // Clean editor: Save is offered but disabled.
    expect(isDisabled(screen.getByRole('button', { name: 'Save' }))).toBe(true);
    expect(screen.getByRole('button', { name: 'Save as new version' })).toBeInTheDocument();
  });

  it('saves the changed files with the loaded revision and offers a rebuild of stale architectures (1.4, 1.5, 1.8)', async () => {
    saveSource.mockResolvedValue({
      files: ['src/plugin.c'],
      deleted: [],
      count: 3,
      source_revision: 4,
      stale_architectures: ['arm64_jp5', 'x86_64'],
    });
    getBuilds.mockResolvedValue(buildsView()).mockResolvedValueOnce(buildsView());
    startBuilds.mockResolvedValue(
      buildsView({
        settled: false,
        builds: {
          x86_64: { buildStatus: 'building', logTail: '', prebuilt: false },
          arm64_jp5: { buildStatus: 'building', logTail: '', prebuilt: false },
        },
      })
    );
    await renderDetail(scaffoldDetail());

    editActiveFile('src/plugin.c', PLUGIN_C + '// edited\n');
    const save = screen.getByRole('button', { name: 'Save' });
    expect(isDisabled(save)).toBe(false);
    fireEvent.click(save);

    await waitFor(() =>
      expect(saveSource).toHaveBeenCalledWith('p-1', 1, {
        files: { 'src/plugin.c': PLUGIN_C + '// edited\n' },
        mode: 'merge',
        expected_source_revision: 3,
      })
    );
    // Success notice names the stale architectures and offers Rebuild.
    await screen.findByText('Source saved');
    expect(
      screen.getByText('2 architectures need a rebuild: arm64_jp5, x86_64.')
    ).toBeInTheDocument();
    // The editor rebaselined: Save is disabled again; the header shows the
    // new revision.
    await waitFor(() => expect(isDisabled(screen.getByRole('button', { name: 'Save' }))).toBe(true));
    expect(
      screen.getByText('Source revision 4. Edits are saved in place on this dev version.')
    ).toBeInTheDocument();
    expect(sourceOf('src/plugin.c').value).toBe(PLUGIN_C + '// edited\n');

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild' }));
    await waitFor(() => expect(startBuilds).toHaveBeenCalledWith('p-1', 1, ['arm64_jp5', 'x86_64']));
  });

  it('SOURCE_LOCKED offers Save as new version and keeps the edits (1.6, 1.10)', async () => {
    saveSource.mockRejectedValue(
      new ApiError('Only dev versions can be edited in place', 409, 'SOURCE_LOCKED', {
        lifecycle_state: 'dev',
      })
    );
    createNewVersion.mockResolvedValue({
      plugin: scaffoldDetail({ version: 2, source_revision: 1 }),
      source_revision: 1,
    });
    await renderDetail(scaffoldDetail());

    editActiveFile('src/plugin.c', PLUGIN_C + '// edited\n');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const header = await screen.findByText('This dev version cannot be edited in place');
    // The alert wrapper holds both the message and the action slot.
    const alert = header.closest('[class*="awsui_alert-wrapper"]') as HTMLElement;
    expect(alert).not.toBeNull();
    // Edits are still in the editor.
    expect(sourceOf('src/plugin.c').value).toBe(PLUGIN_C + '// edited\n');

    fireEvent.click(within(alert).getByRole('button', { name: 'Save as new version' }));
    await waitFor(() =>
      expect(createNewVersion).toHaveBeenCalledWith('p-1', 1, {
        files: { 'src/plugin.c': PLUGIN_C + '// edited\n' },
        delete: [],
      })
    );
    // The page reloads the record afterwards.
    await waitFor(() => expect(getPlugin).toHaveBeenCalledTimes(2));
  });

  it('SOURCE_REVISION_CONFLICT offers Reload and keeps the edits (1.12)', async () => {
    saveSource.mockRejectedValue(
      new ApiError('Source revision changed', 409, 'SOURCE_REVISION_CONFLICT', {
        expected: 3,
        actual: 4,
      })
    );
    await renderDetail(scaffoldDetail());

    editActiveFile('meson.build', MESON + "# tweak\n");
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await screen.findByText('The source changed since you loaded it');
    expect(sourceOf('meson.build').value).toBe(MESON + "# tweak\n");

    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    await waitFor(() => expect(getSourceTree).toHaveBeenCalledTimes(2));
  });

  it('SCAFFOLD_INVALID lists every defect (1.10)', async () => {
    saveSource.mockRejectedValue(
      new ApiError('Scaffold source is not buildable', 422, 'SCAFFOLD_INVALID', {
        defects: ['missing meson.build', 'no C source under src/'],
      })
    );
    await renderDetail(scaffoldDetail());

    editActiveFile('meson.build', '');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await screen.findByText('The source does not form a buildable plugin');
    expect(screen.getByText('missing meson.build; no C source under src/')).toBeInTheDocument();
  });

  it('non-dev versions have no in-place Save and describe the lock (1.6)', async () => {
    await renderDetail(scaffoldDetail({ lifecycle_state: 'test' }));

    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Save as new version' })).toBeInTheDocument();
    expect(
      screen.getByText(
        'Source revision 3. This test version is locked; edits are saved as a new version.'
      )
    ).toBeInTheDocument();
  });

  it('asks before leaving with unsaved edits and discards on confirm (1.11)', async () => {
    await renderDetail(scaffoldDetail());

    // Clean: leaving navigates straight away.
    fireEvent.click(screen.getByRole('button', { name: 'Back to library' }));
    expect(navigateMock).toHaveBeenCalledWith('/node-designer');
    navigateMock.mockClear();

    editActiveFile('src/plugin.c', PLUGIN_C + '// edited\n');
    fireEvent.click(screen.getByRole('button', { name: 'Back to library' }));

    const message = await screen.findByText(
      'You have unsaved source edits. Leave this page and discard them?'
    );
    expect(navigateMock).not.toHaveBeenCalled();
    const dialog = message.closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard and leave' }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/node-designer'));
  });
});

describe('PluginDetail build status extras', () => {
  it('marks stale architectures and summarizes the deployable component (1.8, 8.3)', async () => {
    getBuilds.mockResolvedValue(buildsView({ stale_architectures: ['arm64_jp5'] }));
    await renderDetail(scaffoldDetail());

    await waitFor(() => expect(screen.getAllByText('Rebuild required')).toHaveLength(1));
    expect(
      screen.getByText('Deployable component v0.2 (published): arm64_jp5, x86_64')
    ).toBeInTheDocument();
  });

  it('"Fix with AI" seeds the assistant with the failing build log on the named file (5.1)', async () => {
    const log = 'FAILED: src/plugin.c:42:3: error: expected ";" before "}" token';
    getBuilds.mockResolvedValue(
      buildsView({
        builds: {
          x86_64: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
          arm64_jp5: { buildStatus: 'failed', logTail: log, prebuilt: false },
        },
      })
    );
    await renderDetail(scaffoldDetail());

    // The active tab starts elsewhere; the assistant has nothing attached.
    expect(screen.getByRole('tab', { name: 'builds/arm64_jp5/meson.build' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(screen.getByText('Error output (optional)')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: 'Fix arm64_jp5 build with AI' }));

    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'src/plugin.c' })).toHaveAttribute('aria-selected', 'true')
    );
    expect(screen.getByText('Attached error output (build, arm64_jp5)')).toBeInTheDocument();
    expect((screen.getByRole('textbox', { name: 'Error output' }) as HTMLTextAreaElement).value).toBe(log);
    expect(screen.getByRole('button', { name: 'Diagnose and fix' })).toBeInTheDocument();
  });

  it('a simulator-routed Diagnostic_Context is attached on arrival (5.2)', async () => {
    locationState.value = {
      assistDiagnostics: { kind: 'simulation', text: 'Traceback: ZeroDivisionError' },
    };
    await renderDetail(scaffoldDetail());

    expect(screen.getByText('Attached error output (simulation)')).toBeInTheDocument();
    expect((screen.getByRole('textbox', { name: 'Error output' }) as HTMLTextAreaElement).value).toBe(
      'Traceback: ZeroDivisionError'
    );
  });
});

describe('PluginDetail add architectures', () => {
  it('lists only registry architectures not yet requested and builds just the added ones (6.1-6.3, 6.6)', async () => {
    addArchitectures.mockResolvedValue(
      buildsView({
        requested_architectures: ['arm64_jp5', 'arm64_jp7', 'x86_64'],
        builds: {
          x86_64: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
          arm64_jp5: { buildStatus: 'succeeded', logTail: '', prebuilt: false },
          arm64_jp7: { buildStatus: 'building', logTail: '', prebuilt: false },
        },
        settled: false,
      })
    );
    await renderDetail(scaffoldDetail());

    fireEvent.click(screen.getByRole('button', { name: 'Add architectures' }));

    const trigger = screen.getByRole('button', { name: /Select architectures to add/ });
    fireEvent.mouseDown(trigger);
    fireEvent.click(trigger);
    // Registry minus requested: jp4, jp6, jp7 — never x86_64 or jp5.
    expect(await screen.findAllByRole('option')).toHaveLength(3);
    expect(screen.getByRole('option', { name: /arm64 JetPack 4/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /arm64 JetPack 6/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /x86_64/ })).toBeNull();
    expect(screen.queryByRole('option', { name: /JetPack 5/ })).toBeNull();

    const jp7 = screen.getByRole('option', { name: /arm64 JetPack 7/ });
    fireEvent.mouseDown(jp7);
    fireEvent.mouseUp(jp7);
    fireEvent.click(jp7);

    fireEvent.click(screen.getByRole('button', { name: 'Add and build' }));

    await waitFor(() => expect(addArchitectures).toHaveBeenCalledWith('p-1', 1, ['arm64_jp7']));
    await waitFor(() => expect(screen.getByText('arm64 JetPack 7: building')).toBeInTheDocument());
    // Existing builds untouched; panel closed.
    expect(screen.getByText('arm64 JetPack 5: succeeded')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add and build' })).toBeNull();
  });

  it('surfaces BUILD_TARGET_UNAVAILABLE per architecture and keeps the panel open (6.8)', async () => {
    addArchitectures.mockRejectedValue(
      new ApiError('No build target', 400, 'BUILD_TARGET_UNAVAILABLE', {
        rejected: { arm64_jp7: 'no_build_target' },
      })
    );
    await renderDetail(scaffoldDetail());

    fireEvent.click(screen.getByRole('button', { name: 'Add architectures' }));
    await pickOption(/Select architectures to add/, 'arm64 JetPack 7');
    fireEvent.click(screen.getByRole('button', { name: 'Add and build' }));

    await screen.findByText('arm64_jp7: no build target');
    expect(screen.getByRole('button', { name: 'Add and build' })).toBeInTheDocument();
  });

  it('is disabled on prod versions with the reason (6.4)', async () => {
    await renderDetail(scaffoldDetail({ lifecycle_state: 'prod' }));

    expect(isDisabled(screen.getByRole('button', { name: 'Add architectures' }))).toBe(true);
  });

  it('is hidden when every registry architecture is already requested', async () => {
    getBuilds.mockResolvedValue(
      buildsView({ requested_architectures: ['arm64_jp5', 'x86_64'], buildable_architectures: ['x86_64', 'arm64_jp5'] })
    );
    await renderDetail(scaffoldDetail());

    expect(screen.queryByRole('button', { name: 'Add architectures' })).toBeNull();
  });
});

describe('PluginDetail role gating (9.2)', () => {
  it('Viewers get a read-only editor and no mutating actions', async () => {
    authRole.value = 'Viewer';
    await renderDetail(scaffoldDetail());

    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save as new version' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add architectures' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add file' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Code assistant' })).toBeNull();
    expect(screen.getByText('Source revision 3. Read-only.')).toBeInTheDocument();
    expect(sourceOf('builds/arm64_jp5/meson.build')).toHaveAttribute('readonly');
    // The Git section shows state only.
    expect(screen.queryByRole('button', { name: 'Link repository' })).toBeNull();
  });
});
