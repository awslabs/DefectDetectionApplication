/**
 * Bounded multi-file context for the Code_Assistant (custom-node-source-
 * lifecycle 5.6, Property 14).
 *
 * `otherTextFiles` returns the Source_Tree text files other than the
 * active one, dropping the largest files first until the total UTF-8 size
 * fits `limit`. `allPaths` lists every Source_Tree path (text and binary)
 * exactly once so the model can name a Target_File the request omitted.
 */
import type { ScaffoldFiles } from './types';

/** Server-side cap on context.files (code_assist.MAX_CONTEXT_FILES_BYTES). */
export const CONTEXT_FILES_LIMIT_BYTES = 256 * 1024;
/** Server-side cap on the number of context files. */
export const CONTEXT_FILES_LIMIT_COUNT = 64;

export function utf8Length(text: string): number {
  return new TextEncoder().encode(text).length;
}

export function otherTextFiles(
  files: ScaffoldFiles,
  activeFile: string | null,
  limit: number = CONTEXT_FILES_LIMIT_BYTES,
  maxCount: number = CONTEXT_FILES_LIMIT_COUNT
): Record<string, string> {
  const candidates = Object.entries(files)
    .filter(([path]) => path !== activeFile)
    .map(([path, content]) => ({ path, content, size: utf8Length(content) }))
    // Smallest first so the largest are dropped first when over budget.
    .sort((a, b) => a.size - b.size || a.path.localeCompare(b.path));
  const selected: Record<string, string> = {};
  let total = 0;
  let count = 0;
  for (const { path, content, size } of candidates) {
    if (count >= maxCount) break;
    if (total + size > limit) break;
    selected[path] = content;
    total += size;
    count += 1;
  }
  return selected;
}

export function allPaths(files: ScaffoldFiles, binaryPaths: string[] = []): string[] {
  return [...new Set([...Object.keys(files), ...binaryPaths])].sort();
}
