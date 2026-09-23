/**
 * Which Source_Tree file to open for a failed build (custom-node-source-
 * lifecycle 5.1, Property 18).
 *
 * Precedence: the first Source_Tree path mentioned in the log (longest
 * match first so `plugin/gstx.c` beats `x.c`), else the failing
 * architecture's meson configuration for meson/ninja markers, else the C
 * source for compiler markers, else the Frame_Processing_Hook, else the
 * first path. Always returns a member of `paths` (or null for an empty
 * tree).
 */
export const HOOK_FILE = 'plugin/frame_processing_hook.py';

const MESON_MARKERS = /\b(meson|ninja|meson\.build|dependency\(|subproject)\b/i;
const COMPILER_MARKERS = /\b(gcc|cc1|clang|ld|undefined reference|error:|\.c:\d+)/i;

export function pickFileForDiagnostics(
  log: string,
  paths: string[],
  architecture?: string | null
): string | null {
  if (paths.length === 0) return null;
  const sorted = [...paths].sort((a, b) => b.length - a.length || a.localeCompare(b));
  const text = log || '';

  // 1. A listed path mentioned verbatim in the log.
  let best: { path: string; index: number } | null = null;
  for (const path of sorted) {
    const index = text.indexOf(path);
    if (index >= 0 && (best === null || index < best.index)) {
      best = { path, index };
    }
  }
  if (best) return best.path;

  // 2. meson/ninja markers -> the failing arch's build configuration.
  if (MESON_MARKERS.test(text)) {
    const arch = architecture ? `builds/${architecture}/meson.build` : null;
    if (arch && paths.includes(arch)) return arch;
    const anyMeson = paths.find((p) => p.endsWith('/meson.build') || p === 'meson.build');
    if (anyMeson) return anyMeson;
  }

  // 3. compiler markers -> the C source.
  if (COMPILER_MARKERS.test(text)) {
    const cSource = paths.find((p) => p.endsWith('.c') || p.endsWith('.cpp') || p.endsWith('.h'));
    if (cSource) return cSource;
  }

  // 4. the hook, else the first path.
  if (paths.includes(HOOK_FILE)) return HOOK_FILE;
  return [...paths].sort()[0];
}
