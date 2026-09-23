/**
 * Source_Tree / Repository_Path validity (custom-node-source-lifecycle
 * Property 2): the frontend twin of plugin_records.normalize_source_path.
 *
 * A path is valid iff, after trimming and POSIX normalization, it is
 * non-empty, not `.`, does not start with `/` (or `\`) or `..`, and has no
 * `..` segment. Returns the normalized path or null.
 */
export const MAX_SOURCE_PATH_LENGTH = 200;

/** ASCII control characters (incl. DEL): rejected anywhere in a path. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/;

function normalizePosix(path: string): string {
  const segments: string[] = [];
  for (const raw of path.split('/')) {
    if (raw === '' || raw === '.') continue;
    if (raw === '..') {
      if (segments.length > 0 && segments[segments.length - 1] !== '..') {
        segments.pop();
      } else {
        segments.push('..');
      }
      continue;
    }
    segments.push(raw);
  }
  return segments.length === 0 ? '.' : segments.join('/');
}

export function normalizeSourcePath(path: string): string | null {
  if (typeof path !== 'string') return null;
  const stripped = path.trim();
  if (!stripped) return null;
  if (stripped.startsWith('/') || stripped.startsWith('\\')) return null;
  const clean = normalizePosix(stripped);
  if (clean === '.' || clean === '' || clean.startsWith('..')) return null;
  const segments = clean.split('/');
  if (segments.some((segment) => segment === '..')) return null;
  // Normalization must be a fixed point: a segment with leading or trailing
  // whitespace ("0\r/" -> "0\r" -> "0") would normalize differently on the
  // next pass, and control characters never belong in a file name.
  if (segments.some((segment) => segment !== segment.trim())) return null;
  if (CONTROL_CHARS.test(clean)) return null;
  if (clean.length > MAX_SOURCE_PATH_LENGTH) return null;
  return clean;
}

export function isValidSourcePath(path: string): boolean {
  return normalizeSourcePath(path) !== null;
}
