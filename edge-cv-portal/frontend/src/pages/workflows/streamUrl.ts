/**
 * Stream_URL rules for the Workflow_Builder (rtsp-rtmp-stream-cameras
 * Requirements 1.3, 2.1, 2.2).
 *
 * A line-for-line TypeScript port of `check_stream_url` and
 * `normalize_stream_url` in `workflow_core/stream_url.py`, the single
 * source of truth for the catalog constraint, the validator (V11), the
 * Camera_Registry, the deployment override check, and the device. The
 * port returns the same verdict, problem code and message as the Python
 * module for every input: `streamUrlParity.property.test.ts` replays a
 * corpus the Python module generated
 * (`__fixtures__/streamUrlCorpus.json`), and a Python test keeps that
 * corpus current.
 *
 * Two engine differences are ported explicitly rather than inherited:
 * Python's `\s` and `str.strip()` use a different whitespace set from
 * JavaScript's (`PY_WHITESPACE`), and a Python `$` in `fullmatch` is the
 * JavaScript `$` without the multiline flag.
 */

/**
 * The catalog regex for a Stream_Camera_Source_Node `url`, byte-identical
 * to the Python `STREAM_URL_PATTERN` (the served catalog carries it as
 * the parameter's `regex` constraint).
 */
export const STREAM_URL_PATTERN = '^(rtsps?|rtmps?)://[^\\s/@?#]+([/?][^\\s#]*)?$';

/** Accepted schemes per Stream_Camera_Source_Node type (Requirement 2.1). */
export const SCHEMES_BY_NODE_TYPE: Readonly<Record<string, readonly string[]>> = {
  rtsp_camera_source: ['rtsp', 'rtsps'],
  rtmp_stream_source: ['rtmp', 'rtmps'],
};

/** Accepted schemes per Camera_Source (registry) type. */
export const SCHEMES_BY_SOURCE_TYPE: Readonly<Record<string, readonly string[]>> = {
  RTSP: ['rtsp', 'rtsps'],
  RTMP: ['rtmp', 'rtmps'],
};

/** The Stream_Camera_Source_Node types. */
export const STREAM_SOURCE_TYPES: ReadonlySet<string> = new Set(Object.keys(SCHEMES_BY_NODE_TYPE));

/**
 * The accepted schemes of a Stream_Camera_Source_Node type, or undefined
 * for every other type: Python's `SCHEMES_BY_NODE_TYPE.get(type)`. An
 * own-property lookup, because a node type read from a graph is external
 * input, and indexing the map with `constructor` or `__proto__` would
 * return an Object.prototype member that `checkStreamUrl` cannot iterate.
 */
export function schemesForNodeType(typeId: unknown): readonly string[] | undefined {
  return typeof typeId === 'string' && Object.prototype.hasOwnProperty.call(SCHEMES_BY_NODE_TYPE, typeId)
    ? SCHEMES_BY_NODE_TYPE[typeId]
    : undefined;
}

/**
 * Query parameter names that carry secret material, compared
 * case-insensitively (Requirement 2.2, glossary Secret_Query_Parameter).
 */
export const SECRET_QUERY_PARAMETERS: ReadonlySet<string> = new Set([
  'password',
  'passwd',
  'pwd',
  'pass',
  'secret',
  'token',
  'key',
  'apikey',
  'api_key',
  'auth',
  'signature',
  'sig',
  'streamkey',
  'stream_key',
]);

/** Default port per scheme, dropped by `normalizeStreamUrl`. */
export const DEFAULT_PORTS: Readonly<Record<string, number>> = {
  rtsp: 554,
  rtsps: 322,
  rtmp: 1935,
  rtmps: 443,
};

export const STREAM_URL_INVALID = 'invalid_url';
export const STREAM_URL_SCHEME_NOT_ALLOWED = 'scheme_not_allowed';
export const STREAM_URL_NO_HOST = 'no_host';
export const STREAM_URL_USER_INFO = 'user_info';
export const STREAM_URL_SECRET_QUERY_PARAMETER = 'secret_query_parameter';

/** Why a value is not a valid Stream_URL. `message` never echoes a secret. */
export interface StreamUrlProblem {
  code: string;
  message: string;
}

/**
 * Python's whitespace set (`str.isspace()`, and `\s` in a str pattern):
 * JavaScript's `\s` minus U+FEFF, plus U+001C-U+001F and U+0085.
 */
const PY_WHITESPACE = '\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';

/** The catalog pattern with Python's `\s`, applied as Python's `fullmatch`. */
const STREAM_URL_RE = new RegExp(STREAM_URL_PATTERN.split('\\s').join(PY_WHITESPACE));
const WHITESPACE_RE = new RegExp(`[${PY_WHITESPACE}]`);
const LEADING_WHITESPACE_RE = new RegExp(`^[${PY_WHITESPACE}]+`);
const TRAILING_WHITESPACE_RE = new RegExp(`[${PY_WHITESPACE}]+$`);

/** `<scheme>://<rest>`, with an RFC 3986 scheme in any case. */
const SCHEME_SPLIT_RE = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([\s\S]*)$/;

/** Python's `str.strip()`. */
export function pyStrip(text: string): string {
  return text.replace(LEADING_WHITESPACE_RE, '').replace(TRAILING_WHITESPACE_RE, '');
}

function splitScheme(url: string): [string, string] | null {
  const match = SCHEME_SPLIT_RE.exec(url);
  return match === null ? null : [match[1], match[2]];
}

/** Split the part after `scheme://` into (authority, remainder). */
function splitAuthority(rest: string): [string, string] {
  const end = rest.search(/[/?#]/);
  return end === -1 ? [rest, ''] : [rest.slice(0, end), rest.slice(end)];
}

/** Split an authority (no user information) into (host, port | null). */
function splitHostPort(authority: string): [string, string | null] {
  if (authority.startsWith('[')) {
    const closing = authority.indexOf(']');
    if (closing !== -1) {
      const host = authority.slice(0, closing + 1);
      const tail = authority.slice(closing + 1);
      if (tail.startsWith(':')) {
        return [host, tail.slice(1)];
      }
      return [authority, null];
    }
    return [authority, null];
  }
  const colon = authority.lastIndexOf(':');
  if (colon !== -1) {
    return [authority.slice(0, colon), authority.slice(colon + 1)];
  }
  return [authority, null];
}

/** Whether `port` is a non-empty run of ASCII digits. */
function isPortNumber(port: string): boolean {
  return /^[0-9]+$/.test(port);
}

/** The query string of a URL remainder, without its '?' or fragment. */
function queryOf(remainder: string): string {
  const question = remainder.indexOf('?');
  if (question === -1) {
    return '';
  }
  const query = remainder.slice(question + 1);
  const hash = query.indexOf('#');
  return hash === -1 ? query : query.slice(0, hash);
}

/** The first Secret_Query_Parameter name in `query`, as written. */
function secretQueryParameter(query: string): string | null {
  if (query === '') {
    return null;
  }
  for (const pair of query.split(/[&;]/)) {
    if (pair === '') {
      continue;
    }
    const equals = pair.indexOf('=');
    const name = pyStrip(equals === -1 ? pair : pair.slice(0, equals));
    if (name !== '' && SECRET_QUERY_PARAMETERS.has(name.toLowerCase())) {
      return name;
    }
  }
  return null;
}

/** `allowedSchemes` as a lowercase, de-duplicated list. */
function acceptedSchemes(allowedSchemes: string | readonly unknown[]): string[] {
  const candidates: readonly unknown[] =
    typeof allowedSchemes === 'string' ? [allowedSchemes] : allowedSchemes;
  const seen: string[] = [];
  for (const scheme of candidates) {
    if (typeof scheme !== 'string') {
      continue;
    }
    const lowered = pyStrip(scheme).toLowerCase();
    if (lowered !== '' && !seen.includes(lowered)) {
      seen.push(lowered);
    }
  }
  return seen;
}

/**
 * Check `url` as a Stream_URL for the given schemes; `null` when valid.
 *
 * Rejects a value that cannot be parsed, a scheme outside
 * `allowedSchemes`, an empty host, embedded user information, or a
 * Secret_Query_Parameter. Every accepted URL matches the catalog
 * pattern, and the specific codes are reported in preference to the
 * generic `invalid_url`, so the operator is told what to fix.
 */
export function checkStreamUrl(
  url: unknown,
  allowedSchemes: string | readonly unknown[]
): StreamUrlProblem | null {
  const accepted = acceptedSchemes(allowedSchemes);
  const acceptedText = accepted.length > 0 ? accepted.join(', ') : '(none)';

  if (typeof url !== 'string' || pyStrip(url) === '') {
    return {
      code: STREAM_URL_INVALID,
      message:
        'Stream URL is required and must be a non-empty string of the form ' +
        `${accepted.length > 0 ? accepted[0] : 'rtsp'}://host[:port][/path].`,
    };
  }

  const split = splitScheme(url);
  if (split === null) {
    return {
      code: STREAM_URL_INVALID,
      message:
        `Stream URL is not a valid URL: it must start with one of ${acceptedText}:// ` +
        'followed by a host.',
    };
  }
  const [scheme, rest] = split;
  const lowerScheme = scheme.toLowerCase();

  if (!accepted.includes(lowerScheme)) {
    return {
      code: STREAM_URL_SCHEME_NOT_ALLOWED,
      message:
        `Stream URL scheme '${lowerScheme}' is not allowed here; accepted schemes are ` +
        `${acceptedText}.`,
    };
  }

  const [authority, remainder] = splitAuthority(rest);

  if (authority.includes('@')) {
    return {
      code: STREAM_URL_USER_INFO,
      message:
        "Stream URL must not contain embedded user information (the 'user:password@' " +
        "part before the host); credentials belong in the camera's configuration, not " +
        'in the URL.',
    };
  }

  const [host, port] = splitHostPort(authority);
  if (host === '') {
    return {
      code: STREAM_URL_NO_HOST,
      message: `Stream URL must contain a host, for example ${lowerScheme}://192.168.1.64/path.`,
    };
  }

  const secretName = secretQueryParameter(queryOf(remainder));
  if (secretName !== null) {
    return {
      code: STREAM_URL_SECRET_QUERY_PARAMETER,
      message:
        `Stream URL query parameter '${secretName}' carries credentials; credentials ` +
        "belong in the camera's configuration, not in the URL.",
    };
  }

  if (!STREAM_URL_RE.test(url)) {
    if (scheme !== lowerScheme) {
      return {
        code: STREAM_URL_INVALID,
        message: `Stream URL scheme must be lowercase; write '${lowerScheme}' instead of '${scheme}'.`,
      };
    }
    if (url.includes('#')) {
      return { code: STREAM_URL_INVALID, message: "Stream URL must not contain a fragment ('#')." };
    }
    if (WHITESPACE_RE.test(url)) {
      return { code: STREAM_URL_INVALID, message: 'Stream URL must not contain whitespace.' };
    }
    return {
      code: STREAM_URL_INVALID,
      message:
        'Stream URL is not a valid URL: it must be of the form ' +
        `${lowerScheme}://host[:port][/path][?query].`,
    };
  }

  if (port !== null && port !== '' && !isPortNumber(port)) {
    return { code: STREAM_URL_INVALID, message: 'Stream URL port must be numeric.' };
  }

  return null;
}

/**
 * The canonical form of `url` for camera identity: the scheme and host
 * lowercased and an explicit default port dropped; path and query kept
 * byte for byte. Total and idempotent: a value it cannot parse is
 * returned unchanged (strings are stripped first, as in Python).
 */
export function normalizeStreamUrl(url: string): string {
  const text = pyStrip(url);
  const split = splitScheme(text);
  if (split === null) {
    return text;
  }
  const scheme = split[0].toLowerCase();
  const [fullAuthority, remainder] = splitAuthority(split[1]);
  let authority = fullAuthority;
  let userInfo = '';
  if (authority.includes('@')) {
    const at = authority.lastIndexOf('@');
    userInfo = `${authority.slice(0, at)}@`;
    authority = authority.slice(at + 1);
  }
  const [rawHost, port] = splitHostPort(authority);
  const host = rawHost.toLowerCase();
  let normalizedAuthority: string;
  if (port === null || port === '') {
    normalizedAuthority = host;
  } else if (isPortNumber(port) && Number(port) === DEFAULT_PORTS[scheme]) {
    normalizedAuthority = host;
  } else {
    normalizedAuthority = `${host}:${port}`;
  }
  return `${scheme}://${userInfo}${normalizedAuthority}${remainder}`;
}
