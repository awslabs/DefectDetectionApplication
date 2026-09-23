/**
 * Source_Editor state (custom-node-source-lifecycle tasks 10.2, Properties
 * 1 and 17).
 *
 * Pure model of the Detail_Page editor: the baseline Source_Tree as loaded,
 * the current file map, pending deletions, and the binary (read-only)
 * files. `toSaveRequest` reduces the editor state to exactly the PUT
 * .../source body the backend expects — changed-or-added files under
 * `files`, removed paths under `delete`, `replace` mode with the full map
 * iff a deletion is pending (so the server's completeness check is exact),
 * `merge` with only the changed files otherwise.
 */
import type { SaveSourceRequest, ScaffoldFiles, SourceFileEntry } from './types';

export interface BinaryFile {
  file: string;
  size: number;
}

export interface SourceEditorState {
  /** Text files as loaded (or as last saved). */
  baseline: ScaffoldFiles;
  /** Text files as currently edited. */
  files: ScaffoldFiles;
  /** Baseline paths the user deleted (never also in `files`). */
  deleted: string[];
  /** Oversize / non-UTF-8 files: listed, never editable. */
  binary: BinaryFile[];
  /** Source_Revision the baseline was loaded at (optimistic concurrency). */
  sourceRevision: number;
}

export type SourceEditorEvent =
  | { type: 'edit'; path: string; content: string }
  | { type: 'add'; path: string; content?: string }
  | { type: 'delete'; path: string }
  | { type: 'save-succeeded'; sourceRevision: number }
  | { type: 'save-failed' }
  | { type: 'reset'; files: ScaffoldFiles; binary?: BinaryFile[]; sourceRevision: number };

export function fromSourceTree(
  entries: SourceFileEntry[],
  sourceRevision: number
): SourceEditorState {
  const files: ScaffoldFiles = {};
  const binary: BinaryFile[] = [];
  for (const entry of entries) {
    if (entry.content !== undefined && !entry.binary) {
      files[entry.file] = entry.content;
    } else {
      binary.push({ file: entry.file, size: entry.size });
    }
  }
  return { baseline: { ...files }, files, deleted: [], binary, sourceRevision };
}

export function emptyEditorState(): SourceEditorState {
  return { baseline: {}, files: {}, deleted: [], binary: [], sourceRevision: 1 };
}

/** Paths that differ from the baseline: edited, added, or deleted. */
export function dirtyPaths(state: SourceEditorState): string[] {
  const dirty = new Set<string>(state.deleted);
  for (const [path, content] of Object.entries(state.files)) {
    if (!(path in state.baseline) || state.baseline[path] !== content) {
      dirty.add(path);
    }
  }
  return [...dirty].sort();
}

export function isDirty(state: SourceEditorState): boolean {
  return dirtyPaths(state).length > 0;
}

export function sourceEditorReducer(
  state: SourceEditorState,
  event: SourceEditorEvent
): SourceEditorState {
  switch (event.type) {
    case 'edit': {
      if (!(event.path in state.files)) return state;
      if (state.files[event.path] === event.content) return state;
      return { ...state, files: { ...state.files, [event.path]: event.content } };
    }
    case 'add': {
      if (event.path in state.files) return state;
      if (state.binary.some((b) => b.file === event.path)) return state;
      return {
        ...state,
        files: { ...state.files, [event.path]: event.content ?? '' },
        // Re-adding a deleted baseline path un-deletes it.
        deleted: state.deleted.filter((p) => p !== event.path),
      };
    }
    case 'delete': {
      if (!(event.path in state.files)) return state;
      const files = { ...state.files };
      delete files[event.path];
      const deleted =
        event.path in state.baseline && !state.deleted.includes(event.path)
          ? [...state.deleted, event.path].sort()
          : state.deleted;
      return { ...state, files, deleted };
    }
    case 'save-succeeded':
      return {
        ...state,
        baseline: { ...state.files },
        deleted: [],
        sourceRevision: event.sourceRevision,
      };
    case 'save-failed':
      return state;
    case 'reset':
      return {
        baseline: { ...event.files },
        files: { ...event.files },
        deleted: [],
        binary: event.binary ?? state.binary,
        sourceRevision: event.sourceRevision,
      };
  }
}

/**
 * The PUT .../source body for the current state (Property 1). Returns null
 * when nothing is dirty.
 */
export function toSaveRequest(state: SourceEditorState): SaveSourceRequest | null {
  const changed: ScaffoldFiles = {};
  for (const [path, content] of Object.entries(state.files)) {
    if (!(path in state.baseline) || state.baseline[path] !== content) {
      changed[path] = content;
    }
  }
  const hasDeletions = state.deleted.length > 0;
  if (!hasDeletions && Object.keys(changed).length === 0) return null;
  if (hasDeletions) {
    // Replace mode sends the complete text tree so the server's
    // completeness check and stale-object cleanup are exact. Replace would
    // also delete objects absent from the map — including binary files the
    // editor cannot carry — so trees with binary files use merge + explicit
    // deletions instead.
    if (state.binary.length === 0) {
      return {
        files: { ...state.files },
        delete: [...state.deleted],
        mode: 'replace',
        expected_source_revision: state.sourceRevision,
      };
    }
    return {
      files: changed,
      delete: [...state.deleted],
      mode: 'merge',
      expected_source_revision: state.sourceRevision,
    };
  }
  return {
    files: changed,
    mode: 'merge',
    expected_source_revision: state.sourceRevision,
  };
}

/** Apply a save request to a baseline (the oracle used by Property 1). */
export function applySaveRequest(
  baseline: ScaffoldFiles,
  request: SaveSourceRequest
): ScaffoldFiles {
  const result: ScaffoldFiles =
    request.mode === 'replace' ? {} : { ...baseline };
  for (const [path, content] of Object.entries(request.files)) {
    result[path] = content;
  }
  for (const path of request.delete ?? []) {
    delete result[path];
  }
  return result;
}
