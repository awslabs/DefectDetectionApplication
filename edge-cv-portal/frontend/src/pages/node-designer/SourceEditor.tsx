/**
 * Source_Editor (custom-node-source-lifecycle, Requirements 1.1-1.3,
 * 1.11, 1.13, 5.6, 5.11, 9.2).
 *
 * Tabbed editor over a Plugin_Version's Source_Tree: one Cloudscape tab
 * per text file (dirty tabs marked), a plain Textarea editor (the same
 * surface the CreateWizard/GeneratePanel use), add-file and delete-file
 * actions, read-only tabs for binary/oversize files, and the shared
 * CodeAssistPanel under every editable tab with the multi-file context
 * and the `plugin_source` / `frame_hook` contract chosen per file.
 *
 * The component is presentational over `SourceEditorState`: the owner
 * (PluginDetail) holds the reducer, saves, and decides read-only.
 */
import { useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  FormField,
  Input,
  Modal,
  SpaceBetween,
  Tabs,
  Textarea,
} from '@cloudscape-design/components';
import CodeAssistPanel from '../../components/code-assist/CodeAssistPanel';
import type { CodeAssistDiagnosticsState } from '../../components/code-assist/codeAssistState';
import ConfirmationModal from '../../components/ConfirmationModal';
import { HOOK_FILE } from './diagnosticsFile';
import { allPaths, otherTextFiles } from './fileContext';
import { isValidSourcePath, normalizeSourcePath } from './sourcePath';
import { dirtyPaths, type SourceEditorEvent, type SourceEditorState } from './sourceEditorState';
import type { RecordKind } from './types';

export interface SourceEditorAssist {
  usecaseId: string;
  kind: RecordKind;
  parameters?: { name: string; param_type: string; description?: string }[];
  /** Diagnostic_Context seeded by "Fix with AI" (5.1, 5.2). */
  diagnostics?: CodeAssistDiagnosticsState | null;
}

export interface SourceEditorProps {
  state: SourceEditorState;
  dispatch: (event: SourceEditorEvent) => void;
  activeFile: string | null;
  onActiveFileChange: (path: string) => void;
  /** Read-only roles / locked states: no editing, adding, deleting, or assistant. */
  readOnly: boolean;
  /** When present, the Code_Assistant renders under every editable tab. */
  assist?: SourceEditorAssist | null;
  /** True when the source was loaded partially (bulk read truncated). */
  truncated?: boolean;
}

const CODE_STYLE: React.CSSProperties = {
  fontFamily: 'Monaco, Menlo, "Ubuntu Mono", monospace',
};

function formatSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${size} B`;
}

export default function SourceEditor({
  state,
  dispatch,
  activeFile,
  onActiveFileChange,
  readOnly,
  assist,
  truncated,
}: SourceEditorProps) {
  const [addOpen, setAddOpen] = useState(false);
  const [addPath, setAddPath] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const textPaths = Object.keys(state.files).sort();
  const binaryPaths = state.binary.map((b) => b.file).sort();
  const dirty = new Set(dirtyPaths(state));
  const effectiveActive =
    activeFile && (activeFile in state.files || binaryPaths.includes(activeFile))
      ? activeFile
      : textPaths[0] ?? binaryPaths[0] ?? null;

  const addPathError = addPath.trim()
    ? !isValidSourcePath(addPath)
      ? 'Use a relative path without ".." segments (for example docs/NOTES.md).'
      : (normalizeSourcePath(addPath) as string) in state.files ||
          binaryPaths.includes(normalizeSourcePath(addPath) as string)
        ? 'A file with this path already exists.'
        : null
    : null;

  const submitAdd = () => {
    const clean = normalizeSourcePath(addPath);
    if (!clean || addPathError) return;
    dispatch({ type: 'add', path: clean, content: '' });
    onActiveFileChange(clean);
    setAddOpen(false);
    setAddPath('');
  };

  const confirmDelete = () => {
    if (!deleteTarget) return;
    dispatch({ type: 'delete', path: deleteTarget });
    const remaining = textPaths.filter((p) => p !== deleteTarget);
    if (effectiveActive === deleteTarget && remaining.length > 0) {
      onActiveFileChange(remaining[0]);
    }
    setDeleteTarget(null);
  };

  const tabs = [
    ...textPaths.map((path) => {
      const isDirty = dirty.has(path);
      const content = state.files[path];
      const contract = path === HOOK_FILE ? ('frame_hook' as const) : ('plugin_source' as const);
      return {
        id: path,
        label: isDirty ? `${path} •` : path,
        content: (
          <SpaceBetween size="s">
            <SpaceBetween direction="horizontal" size="xs">
              {isDirty && <Badge color="blue">Modified</Badge>}
              {!readOnly && (
                <Button
                  variant="inline-link"
                  ariaLabel={`Delete ${path}`}
                  onClick={() => setDeleteTarget(path)}
                >
                  Delete file
                </Button>
              )}
            </SpaceBetween>
            <div style={CODE_STYLE}>
              <Textarea
                value={content}
                onChange={({ detail }) => dispatch({ type: 'edit', path, content: detail.value })}
                rows={24}
                spellcheck={false}
                readOnly={readOnly}
                ariaLabel={`Source of ${path}`}
              />
            </div>
            {!readOnly && assist && (
              <CodeAssistPanel
                usecaseId={assist.usecaseId}
                surface="node-designer"
                contract={contract}
                context={{
                  parameters: assist.parameters,
                  active_file: path,
                  files: otherTextFiles(state.files, path),
                  file_paths: allPaths(state.files, binaryPaths),
                  kind: assist.kind,
                }}
                editorCode={content}
                activeFile={path}
                diagnostics={assist.diagnostics}
                onAccept={(code, targetFile) => {
                  const target = targetFile ?? path;
                  if (target in state.files) {
                    dispatch({ type: 'edit', path: target, content: code });
                  } else {
                    dispatch({ type: 'add', path: target, content: code });
                  }
                  if (target !== path) onActiveFileChange(target);
                }}
              />
            )}
          </SpaceBetween>
        ),
      };
    }),
    ...state.binary.map((entry) => ({
      id: entry.file,
      label: entry.file,
      content: (
        <Alert type="info" header="Read-only file">
          {`${entry.file} (${formatSize(entry.size)}) is a binary or oversized file and cannot be edited in the portal.`}
        </Alert>
      ),
    })),
  ];

  return (
    <SpaceBetween size="s">
      {truncated && (
        <Alert type="warning" header="Source partially loaded">
          The source tree is larger than the inline budget; some files are listed without content
          and cannot be edited here.
        </Alert>
      )}
      {!readOnly && (
        <SpaceBetween direction="horizontal" size="xs">
          <Button iconName="add-plus" onClick={() => setAddOpen(true)}>
            Add file
          </Button>
          {dirty.size > 0 && (
            <Box variant="span" color="text-status-info">
              {dirty.size} unsaved {dirty.size === 1 ? 'change' : 'changes'}
            </Box>
          )}
        </SpaceBetween>
      )}
      {tabs.length === 0 ? (
        <Box color="text-status-inactive">No source files.</Box>
      ) : (
        <Tabs
          activeTabId={effectiveActive ?? undefined}
          onChange={({ detail }) => onActiveFileChange(detail.activeTabId)}
          tabs={tabs}
        />
      )}

      <Modal
        visible={addOpen}
        header="Add file"
        onDismiss={() => setAddOpen(false)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setAddOpen(false)}>Cancel</Button>
              <Button
                variant="primary"
                disabled={!addPath.trim() || Boolean(addPathError)}
                onClick={submitAdd}
              >
                Add
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormField
          label="File path"
          description="Relative to the plugin source root."
          errorText={addPathError ?? undefined}
        >
          <Input
            value={addPath}
            onChange={({ detail }) => setAddPath(detail.value)}
            placeholder="docs/NOTES.md"
            ariaLabel="New file path"
            onKeyDown={({ detail }) => {
              if (detail.key === 'Enter') submitAdd();
            }}
          />
        </FormField>
      </Modal>

      <ConfirmationModal
        visible={deleteTarget !== null}
        title={`Delete ${deleteTarget ?? ''}`}
        message={`Remove ${deleteTarget ?? 'this file'} from the plugin source? The deletion is applied when you save.`}
        confirmButtonText="Delete"
        variant="danger"
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </SpaceBetween>
  );
}
