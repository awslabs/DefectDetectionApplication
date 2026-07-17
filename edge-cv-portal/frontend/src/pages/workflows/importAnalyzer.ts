/**
 * Import_Analyzer — pure import extraction for custom Python node
 * module code (Requirements 3.1, 3.3, 3.10).
 *
 * `extractImports` is a line/continuation-aware scanner, not a full
 * Python parser: it strips comments and string literals, joins
 * explicit (backslash) and implicit (open bracket) line continuations,
 * and matches every logical line — at any indentation, so imports
 * nested in function bodies and conditional blocks count — against the
 * import-statement grammar. "Cannot be parsed" (Requirement 3.10) is
 * defined as an unterminated string literal, an unterminated bracket,
 * or a line starting with `import`/`from` that does not match the
 * grammar; syntax errors outside import statements cannot change the
 * import set and are ignored.
 */

/** Result of scanning module code for import statements. */
export type ImportScan =
  | { ok: true; imports: string[] } // absolute top-level module names, deduped
  | { ok: false }; // unparseable (Requirement 3.10)

/** Python identifier (approximated with Unicode letter/number classes). */
const ID = '[\\p{L}_][\\p{L}\\p{N}_]*';
/** Dotted module path: `a.b.c`. */
const DOTTED = `${ID}(?:\\.${ID})*`;
/** One `import` target: dotted module with optional alias. */
const IMPORT_ITEM = `${DOTTED}(?:\\s+as\\s+${ID})?`;
/** `from … import` name list: names with optional aliases. */
const NAME_LIST = `${ID}(?:\\s+as\\s+${ID})?(?:\\s*,\\s*${ID}(?:\\s+as\\s+${ID})?)*`;

/** `import a.b.c as x, d` — top-level names `a`, `d`. */
const IMPORT_RE = new RegExp(`^import\\s+${IMPORT_ITEM}(?:\\s*,\\s*${IMPORT_ITEM})*$`, 'u');

/**
 * `from a.b import x, y` / `from . import x` / `from a import *` /
 * `from a import (x, y,)` — group 1 captures the module part (leading
 * dots mean a relative import, which yields no absolute name).
 */
const FROM_RE = new RegExp(
  `^from\\s+(\\.+\\s*(?:${DOTTED})?|${DOTTED})\\s+import\\s+` +
    `(?:\\*|${NAME_LIST}|\\(\\s*${NAME_LIST}\\s*,?\\s*\\))$`,
  'u'
);

/** Does this logical line start with the `import` or `from` keyword? */
const IMPORT_KEYWORD_RE = /^(?:import|from)\b/u;

/**
 * Phase 1: remove comments and string literal contents from the source
 * while preserving newlines outside literals (a triple-quoted string's
 * embedded newlines vanish with it, correctly keeping its enclosing
 * logical line intact). Returns `null` when a string literal is
 * unterminated (Requirement 3.10).
 */
function stripCommentsAndStrings(code: string): string | null {
  let out = '';
  let i = 0;
  const n = code.length;
  while (i < n) {
    const ch = code[i];
    if (ch === '#') {
      // Comment: skip to end of line (newline itself is kept).
      while (i < n && code[i] !== '\n') {
        i += 1;
      }
    } else if (ch === '"' || ch === "'") {
      const quote = ch;
      const triple = code[i + 1] === quote && code[i + 2] === quote;
      i += triple ? 3 : 1;
      let terminated = false;
      while (i < n) {
        if (code[i] === '\\') {
          // A backslash never terminates a string (raw strings
          // included), so skipping the escaped character is safe.
          i += 2;
        } else if (triple) {
          if (code[i] === quote && code[i + 1] === quote && code[i + 2] === quote) {
            i += 3;
            terminated = true;
            break;
          }
          i += 1;
        } else if (code[i] === quote) {
          i += 1;
          terminated = true;
          break;
        } else if (code[i] === '\n') {
          // Single-quoted strings cannot span a raw newline.
          break;
        } else {
          i += 1;
        }
      }
      if (!terminated) {
        return null;
      }
    } else {
      out += ch;
      i += 1;
    }
  }
  return out;
}

/**
 * Phase 2: split the stripped source into logical lines, joining
 * explicit backslash continuations and implicit continuations inside
 * open brackets. Returns `null` when a bracket is left unterminated
 * (Requirement 3.10).
 */
function toLogicalLines(stripped: string): string[] | null {
  const lines: string[] = [];
  let current = '';
  let depth = 0;
  let i = 0;
  const n = stripped.length;
  while (i < n) {
    const ch = stripped[i];
    if (ch === '\\') {
      // Explicit continuation: backslash immediately before the line end.
      let j = i + 1;
      if (stripped[j] === '\r') {
        j += 1;
      }
      if (stripped[j] === '\n') {
        current += ' ';
        i = j + 1;
        continue;
      }
      current += ch;
      i += 1;
    } else if (ch === '(' || ch === '[' || ch === '{') {
      depth += 1;
      current += ch;
      i += 1;
    } else if (ch === ')' || ch === ']' || ch === '}') {
      depth = Math.max(0, depth - 1);
      current += ch;
      i += 1;
    } else if (ch === '\n') {
      if (depth > 0) {
        // Implicit continuation inside brackets.
        current += ' ';
      } else {
        lines.push(current);
        current = '';
      }
      i += 1;
    } else if (ch === '\r') {
      i += 1;
    } else {
      current += ch;
      i += 1;
    }
  }
  if (depth > 0) {
    return null;
  }
  lines.push(current);
  return lines;
}

/** First segment of a dotted module path: `a.b.c` → `a`. */
function topLevelName(dotted: string): string {
  const dot = dotted.indexOf('.');
  return dot === -1 ? dotted : dotted.slice(0, dot);
}

/**
 * Scan module code for import statements and return the deduped
 * absolute top-level module names they import (Requirement 3.1).
 * Relative imports (`from . import x`, `from .sib import x`) are
 * recognized but excluded (Requirement 3.3). Returns `{ok: false}`
 * when the code cannot be parsed: an unterminated string or bracket,
 * or a line starting with `import`/`from` that does not match the
 * import grammar (Requirement 3.10).
 */
export function extractImports(code: string): ImportScan {
  const stripped = stripCommentsAndStrings(code);
  if (stripped === null) {
    return { ok: false };
  }
  const lines = toLogicalLines(stripped);
  if (lines === null) {
    return { ok: false };
  }

  const imports: string[] = [];
  const seen = new Set<string>();
  const record = (name: string): void => {
    if (!seen.has(name)) {
      seen.add(name);
      imports.push(name);
    }
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!IMPORT_KEYWORD_RE.test(trimmed)) {
      continue;
    }
    if (trimmed.startsWith('import')) {
      if (!IMPORT_RE.test(trimmed)) {
        return { ok: false };
      }
      // `import a.b.c as x, d` → `a`, `d`
      const body = trimmed.slice('import'.length);
      for (const part of body.split(',')) {
        const module = part.trim().split(/\s+as\s+/u)[0].trim();
        record(topLevelName(module));
      }
    } else {
      const match = FROM_RE.exec(trimmed);
      if (match === null) {
        return { ok: false };
      }
      const module = match[1];
      if (!module.startsWith('.')) {
        // `from a.b import x, y` → `a`; relative imports are excluded.
        record(topLevelName(module));
      }
    }
  }

  return { ok: true, imports };
}

/* ------------------------------------------------------------------ */
/* Requirements derivation (Requirements 3.2, 3.3, 3.7)               */
/* ------------------------------------------------------------------ */

/** One derived requirements entry. */
export interface DerivedRequirement {
  /** pip distribution name */
  distribution: string;
  /** true when the import had no mapping (Requirement 3.7) */
  needsReview: boolean;
}

/**
 * Import_Mapping: import (module) name → pip distribution name.
 * Identity entries are listed explicitly where the requirements name
 * them (`numpy` per Requirement 3.2) or where they are common in this
 * domain; anything absent falls through to the identity-plus-review
 * rule, so the table only ever improves accuracy.
 */
export const IMPORT_MAPPING: Record<string, string> = {
  cv2: 'opencv-python-headless',
  PIL: 'Pillow',
  sklearn: 'scikit-learn',
  skimage: 'scikit-image',
  yaml: 'PyYAML',
  bs4: 'beautifulsoup4',
  dateutil: 'python-dateutil',
  dotenv: 'python-dotenv',
  serial: 'pyserial',
  usb: 'pyusb',
  zmq: 'pyzmq',
  paho: 'paho-mqtt',
  tflite_runtime: 'tflite-runtime',
  numpy: 'numpy',
  scipy: 'scipy',
  pandas: 'pandas',
  requests: 'requests',
  matplotlib: 'matplotlib',
  torch: 'torch',
  torchvision: 'torchvision',
  onnxruntime: 'onnxruntime',
  boto3: 'boto3',
};

/**
 * Python standard-library top-level module names: the union of CPython
 * 3.9 and 3.11 `sys.stdlib_module_names`, plus `__future__` (already a
 * member). The 3.9-only entries (removed by 3.11) are `_bootlocale`,
 * `_peg_parser`, `binhex`, `formatter`, `parser`, and `symbol`.
 * Imports of these names never yield a pip requirement
 * (Requirement 3.3).
 */
export const STDLIB_MODULES: ReadonlySet<string> = new Set([
  '__future__',
  '_abc',
  '_aix_support',
  '_ast',
  '_asyncio',
  '_bisect',
  '_blake2',
  '_bootlocale',
  '_bootsubprocess',
  '_bz2',
  '_codecs',
  '_codecs_cn',
  '_codecs_hk',
  '_codecs_iso2022',
  '_codecs_jp',
  '_codecs_kr',
  '_codecs_tw',
  '_collections',
  '_collections_abc',
  '_compat_pickle',
  '_compression',
  '_contextvars',
  '_crypt',
  '_csv',
  '_ctypes',
  '_curses',
  '_curses_panel',
  '_datetime',
  '_dbm',
  '_decimal',
  '_elementtree',
  '_frozen_importlib',
  '_frozen_importlib_external',
  '_functools',
  '_gdbm',
  '_hashlib',
  '_heapq',
  '_imp',
  '_io',
  '_json',
  '_locale',
  '_lsprof',
  '_lzma',
  '_markupbase',
  '_md5',
  '_msi',
  '_multibytecodec',
  '_multiprocessing',
  '_opcode',
  '_operator',
  '_osx_support',
  '_overlapped',
  '_peg_parser',
  '_pickle',
  '_posixshmem',
  '_posixsubprocess',
  '_py_abc',
  '_pydecimal',
  '_pyio',
  '_queue',
  '_random',
  '_scproxy',
  '_sha1',
  '_sha256',
  '_sha3',
  '_sha512',
  '_signal',
  '_sitebuiltins',
  '_socket',
  '_sqlite3',
  '_sre',
  '_ssl',
  '_stat',
  '_statistics',
  '_string',
  '_strptime',
  '_struct',
  '_symtable',
  '_thread',
  '_threading_local',
  '_tkinter',
  '_tokenize',
  '_tracemalloc',
  '_typing',
  '_uuid',
  '_warnings',
  '_weakref',
  '_weakrefset',
  '_winapi',
  '_zoneinfo',
  'abc',
  'aifc',
  'antigravity',
  'argparse',
  'array',
  'ast',
  'asynchat',
  'asyncio',
  'asyncore',
  'atexit',
  'audioop',
  'base64',
  'bdb',
  'binascii',
  'binhex',
  'bisect',
  'builtins',
  'bz2',
  'cProfile',
  'calendar',
  'cgi',
  'cgitb',
  'chunk',
  'cmath',
  'cmd',
  'code',
  'codecs',
  'codeop',
  'collections',
  'colorsys',
  'compileall',
  'concurrent',
  'configparser',
  'contextlib',
  'contextvars',
  'copy',
  'copyreg',
  'crypt',
  'csv',
  'ctypes',
  'curses',
  'dataclasses',
  'datetime',
  'dbm',
  'decimal',
  'difflib',
  'dis',
  'distutils',
  'doctest',
  'email',
  'encodings',
  'ensurepip',
  'enum',
  'errno',
  'faulthandler',
  'fcntl',
  'filecmp',
  'fileinput',
  'fnmatch',
  'formatter',
  'fractions',
  'ftplib',
  'functools',
  'gc',
  'genericpath',
  'getopt',
  'getpass',
  'gettext',
  'glob',
  'graphlib',
  'grp',
  'gzip',
  'hashlib',
  'heapq',
  'hmac',
  'html',
  'http',
  'idlelib',
  'imaplib',
  'imghdr',
  'imp',
  'importlib',
  'inspect',
  'io',
  'ipaddress',
  'itertools',
  'json',
  'keyword',
  'lib2to3',
  'linecache',
  'locale',
  'logging',
  'lzma',
  'mailbox',
  'mailcap',
  'marshal',
  'math',
  'mimetypes',
  'mmap',
  'modulefinder',
  'msilib',
  'msvcrt',
  'multiprocessing',
  'netrc',
  'nis',
  'nntplib',
  'nt',
  'ntpath',
  'nturl2path',
  'numbers',
  'opcode',
  'operator',
  'optparse',
  'os',
  'ossaudiodev',
  'parser',
  'pathlib',
  'pdb',
  'pickle',
  'pickletools',
  'pipes',
  'pkgutil',
  'platform',
  'plistlib',
  'poplib',
  'posix',
  'posixpath',
  'pprint',
  'profile',
  'pstats',
  'pty',
  'pwd',
  'py_compile',
  'pyclbr',
  'pydoc',
  'pydoc_data',
  'pyexpat',
  'queue',
  'quopri',
  'random',
  're',
  'readline',
  'reprlib',
  'resource',
  'rlcompleter',
  'runpy',
  'sched',
  'secrets',
  'select',
  'selectors',
  'shelve',
  'shlex',
  'shutil',
  'signal',
  'site',
  'smtpd',
  'smtplib',
  'sndhdr',
  'socket',
  'socketserver',
  'spwd',
  'sqlite3',
  'sre_compile',
  'sre_constants',
  'sre_parse',
  'ssl',
  'stat',
  'statistics',
  'string',
  'stringprep',
  'struct',
  'subprocess',
  'sunau',
  'symbol',
  'symtable',
  'sys',
  'sysconfig',
  'syslog',
  'tabnanny',
  'tarfile',
  'telnetlib',
  'tempfile',
  'termios',
  'textwrap',
  'this',
  'threading',
  'time',
  'timeit',
  'tkinter',
  'token',
  'tokenize',
  'tomllib',
  'trace',
  'traceback',
  'tracemalloc',
  'tty',
  'turtle',
  'turtledemo',
  'types',
  'typing',
  'unicodedata',
  'unittest',
  'urllib',
  'uu',
  'uuid',
  'venv',
  'warnings',
  'wave',
  'weakref',
  'webbrowser',
  'winreg',
  'winsound',
  'wsgiref',
  'xdrlib',
  'xml',
  'xmlrpc',
  'zipapp',
  'zipfile',
  'zipimport',
  'zlib',
  'zoneinfo',
]);

/**
 * Runtime-provided helper module: importable on the edge device
 * without a pip install, so never a requirement (Requirement 3.3).
 */
const RUNTIME_PROVIDED_MODULES: ReadonlySet<string> = new Set(['dda_frames']);

/**
 * Derive pip requirements entries from imported top-level module
 * names (Requirements 3.2, 3.3, 3.7): standard-library names and
 * `dda_frames` produce no entry; names present in the Import_Mapping
 * produce their mapped distribution with `needsReview: false`; every
 * other name produces itself with `needsReview: true`. Output is
 * sorted by distribution and deduped (a mapped occurrence wins over an
 * unmapped one for the same distribution).
 */
export function deriveRequirements(imports: string[]): DerivedRequirement[] {
  const byDistribution = new Map<string, DerivedRequirement>();
  for (const name of imports) {
    if (STDLIB_MODULES.has(name) || RUNTIME_PROVIDED_MODULES.has(name)) {
      continue;
    }
    // hasOwnProperty guards against prototype-chain keys (a Python
    // module could legally be named `constructor` or `toString`).
    const mapped = Object.prototype.hasOwnProperty.call(IMPORT_MAPPING, name);
    const distribution = mapped ? IMPORT_MAPPING[name] : name;
    const needsReview = !mapped;
    const existing = byDistribution.get(distribution);
    if (existing === undefined) {
      byDistribution.set(distribution, { distribution, needsReview });
    } else if (existing.needsReview && !needsReview) {
      byDistribution.set(distribution, { distribution, needsReview });
    }
  }
  return [...byDistribution.values()].sort((a, b) =>
    a.distribution < b.distribution ? -1 : a.distribution > b.distribution ? 1 : 0
  );
}

/* ------------------------------------------------------------------ */
/* Requirements parsing, rendering, and reconciliation                */
/* (Requirements 3.5, 3.9)                                            */
/* ------------------------------------------------------------------ */

/**
 * Trailing comment marking a requirements line as derived from code
 * imports. Any line without this marker is manual and is never
 * touched by reconciliation (Requirement 3.5).
 */
export const DERIVED_MARKER = '# via code imports';

/** Suffix on derived lines whose import had no mapping (Requirement 3.7). */
const NEEDS_REVIEW_SUFFIX = '(verify package name)';

/** Full marker carried by derived lines that need review. */
const DERIVED_MARKER_NEEDS_REVIEW = `${DERIVED_MARKER} ${NEEDS_REVIEW_SUFFIX}`;

/** One parsed line of a requirements text. */
export interface RequirementsEntry {
  /** the verbatim line (manual lines round-trip exactly) */
  raw: string;
  /** normalized (PEP 503) name, null for blank/comment lines */
  distribution: string | null;
  /** line carries DERIVED_MARKER */
  derived: boolean;
  /** derived line carries the verify suffix */
  needsReview: boolean;
}

/**
 * PEP 503 normalization of a distribution name: runs of `-`, `_`, and
 * `.` collapse to a single `-`, and the result is lowercased.
 */
function normalizeDistribution(name: string): string {
  return name.replace(/[-_.]+/g, '-').toLowerCase();
}

/**
 * PEP 508-style leading project name on a requirement line: begins and
 * ends with an alphanumeric, with `-`/`_`/`.` allowed inside. Version
 * pins, extras, and markers follow the name and are ignored here.
 */
const DISTRIBUTION_NAME_RE = /^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/;

/** Parse a single requirements line into a RequirementsEntry. */
function parseLine(raw: string): RequirementsEntry {
  const derived = raw.includes(DERIVED_MARKER);
  const needsReview = derived && raw.includes(DERIVED_MARKER_NEEDS_REVIEW);

  // The distribution name precedes any comment; `#` cannot occur in a
  // name, so cutting at the first `#` is safe.
  const hash = raw.indexOf('#');
  const beforeComment = (hash === -1 ? raw : raw.slice(0, hash)).trim();
  const match = DISTRIBUTION_NAME_RE.exec(beforeComment);
  const distribution = match === null ? null : normalizeDistribution(match[1]);

  return { raw, distribution, derived, needsReview };
}

/**
 * Parse a requirements text into entries, one per line, preserving
 * every line verbatim in `raw` so that `renderRequirements` is an
 * exact inverse. An empty text parses to no entries.
 */
export function parseRequirements(text: string): RequirementsEntry[] {
  if (text === '') {
    return [];
  }
  return text.split('\n').map(parseLine);
}

/** Render entries back to a requirements text (inverse of parse). */
export function renderRequirements(entries: RequirementsEntry[]): string {
  return entries.map((entry) => entry.raw).join('\n');
}

/** Render one derived entry as a marker line. */
function renderDerivedLine(entry: DerivedRequirement): string {
  const marker = entry.needsReview ? DERIVED_MARKER_NEEDS_REVIEW : DERIVED_MARKER;
  return `${entry.distribution}  ${marker}`;
}

/**
 * Merge a freshly derived requirements list into the current
 * requirements text (Requirements 3.5, 3.9):
 *
 * 1. Every non-derived line (manual entries, pins, user comments,
 *    blank lines) is kept verbatim and in order.
 * 2. Every previously derived line (marker present) is dropped.
 * 3. One marker line is appended per derived entry whose PEP
 *    503-normalized distribution matches no surviving manual entry's
 *    distribution (Requirement 3.9).
 *
 * The function is idempotent for a fixed derived list and never
 * touches manual text.
 */
export function reconcileRequirements(
  currentText: string,
  derived: DerivedRequirement[]
): string {
  const surviving = parseRequirements(currentText).filter((entry) => !entry.derived);

  const manualDistributions = new Set<string>();
  for (const entry of surviving) {
    if (entry.distribution !== null) {
      manualDistributions.add(entry.distribution);
    }
  }

  const lines = surviving.map((entry) => entry.raw);
  const appended = new Set<string>();
  for (const entry of derived) {
    const normalized = normalizeDistribution(entry.distribution);
    if (manualDistributions.has(normalized) || appended.has(normalized)) {
      continue;
    }
    appended.add(normalized);
    lines.push(renderDerivedLine(entry));
  }

  return lines.join('\n');
}
