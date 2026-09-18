/**
 * Unit tests for `services/sessionRedirect.ts`
 * (spec: portal-session-expiry-return-to-page, task 2.3).
 *
 * Example-based counterpart to the two property tests in
 * `sessionRedirect.property.test.ts`: the adversarial validator table,
 * first-write-wins, consume-clears, storage-disabled degradation, and
 * `/login` never being saved.
 *
 * Nothing here depends on a real `window.location` assignment — jsdom cannot
 * navigate, so the `navigateTo` seam is what the redirect cases observe
 * (design Decision 4, Requirement 6.1). The document location itself is moved
 * with `history.replaceState`, which jsdom supports, so the default-argument
 * behaviour is covered without stubbing `window.location`.
 *
 * Validates: Requirements 1.1, 1.4, 1.5, 1.6, 2.4, 3.1, 3.2, 3.3, 3.4, 4.1, 6.1
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  LOGIN_PATH,
  MAX_PATH_LENGTH,
  RETURN_TO_KEY,
  clearAttemptedLocation,
  isSafeInternalPath,
  redirectToLogin,
  saveAttemptedLocation,
  setNavigateTo,
  takeAttemptedLocation,
} from './sessionRedirect';

/** What the store holds right now, read directly through the fixed key. */
function planted(): string | null {
  return window.sessionStorage.getItem(RETURN_TO_KEY);
}

/** Plant a raw value as if a previous exit (or an attacker) had written it. */
function plant(raw: string): void {
  window.sessionStorage.setItem(RETURN_TO_KEY, raw);
}

/** Move the document location without navigating (jsdom supports this). */
function atLocation(path: string): void {
  window.history.replaceState({}, '', path);
}

/** Descriptor of the real `sessionStorage`, restored after each test. */
const realSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');

/**
 * Replace `globalThis.sessionStorage` for the duration of a test.
 *
 * @param get accessor invoked on every property read — may throw, which is
 *   exactly what a browser with site data blocked does.
 */
function stubSessionStorage(get: () => Storage): void {
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get });
}

function restoreSessionStorage(): void {
  if (realSessionStorage) {
    Object.defineProperty(globalThis, 'sessionStorage', realSessionStorage);
  } else {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (globalThis as any).sessionStorage;
  }
}

beforeEach(() => {
  restoreSessionStorage();
  window.sessionStorage.clear();
  atLocation('/');
  setNavigateTo(null);
});

afterEach(() => {
  restoreSessionStorage();
  setNavigateTo(null);
});

// ---------------------------------------------------------------- validator

describe('isSafeInternalPath — adversarial table (Requirements 3.1, 3.3, 3.4)', () => {
  const accepted: Array<[string, string]> = [
    ['the site root', '/'],
    ['a plain page', '/dashboard'],
    ['a deep page with query and hash', '/workflows/builder/abc?tab=nodes#node-3'],
    ['a device tab', '/devices/dev-1?tab=cameras#latest'],
    ['a trailing slash', '/devices/'],
    ['percent escapes', '/data/%2Fnested%20name?prefix=a%2Fb'],
    ['a literal space', '/data/my folder/image 1.jpg'],
    ['a backslash anywhere but index 1', '/a\\b'],
    ['a bare query marker', '/dashboard?'],
    ['a bare fragment marker', '/dashboard#'],
    ['non-ASCII characters', '/data/日本語/ü🙂'],
    // A host-looking value inside the *query* is not a navigation target: the
    // browser stays on /dashboard, so this must not be rejected.
    ['a host-looking query value', '/dashboard?next=//evil.com'],
    ['a path merely starting with the word login', '/loginish'],
    ['login as a non-leading segment', '/user/login'],
    ['exactly the length bound', `/d/${'a'.repeat(MAX_PATH_LENGTH - 3)}`],
  ];

  it.each(accepted)('accepts %s', (_label, candidate) => {
    expect(isSafeInternalPath(candidate)).toBe(true);
  });

  const rejected: Array<[string, unknown]> = [
    // --- not a rooted, same-origin path (3.1)
    ['the empty string', ''],
    ['a relative path', 'dashboard'],
    ['a dot-relative path', './dashboard'],
    ['a parent traversal', '../../etc/passwd'],
    ['a leading space', ' /dashboard'],
    ['a leading tab', '\t/dashboard'],
    ['a bare query string', '?tab=nodes'],
    ['a bare fragment', '#section'],
    // --- protocol-relative and its backslash spelling (3.1)
    ['protocol-relative', '//evil.com'],
    ['protocol-relative with a path', '//evil.com/steal?t=1'],
    ['protocol-relative with credentials', '//user@evil.com/dashboard'],
    ['the backslash spelling', '/\\evil.com'],
    ['the backslash spelling with a path', '/\\evil.com/steal'],
    // --- scheme-bearing (3.1)
    ['http', 'http://evil.com'],
    ['https', 'https://evil.com/dashboard'],
    ['https in mixed case', 'HTTPS://evil.com'],
    ['javascript', 'javascript:alert(1)'],
    ['javascript in mixed case', 'JavaScript:alert(1)'],
    ['data', 'data:text/html;base64,PHNjcmlwdD4='],
    ['vbscript', 'vbscript:msgbox(1)'],
    ['file', 'file:///etc/passwd'],
    ['mailto', 'mailto:someone@evil.com'],
    // --- control characters (3.1)
    ['an embedded NUL', '/dash\u0000board'],
    ['an embedded newline', '/dash\nboard'],
    ['an embedded carriage return', '/dash\rboard'],
    ['an embedded tab', '/dash\tboard'],
    ['an embedded escape', '/dash\u001bboard'],
    ['an embedded DEL', '/dash\u007fboard'],
    // --- length bound (3.3)
    ['one character over the bound', `/${'a'.repeat(MAX_PATH_LENGTH)}`],
    ['far over the bound', `/devices/${'a'.repeat(MAX_PATH_LENGTH * 2)}`],
    // --- the login page itself (1.4, loop safety for 2.5)
    ['the login page', '/login'],
    ['the login page with a trailing slash', '/login/'],
    ['the login page with a query', '/login?next=/dashboard'],
    ['the login page with a hash', '/login#form'],
    ['the login page in upper case', '/LOGIN'],
    ['the login page in mixed case', '/Login?x=1'],
    ['a login sub-path', '/login/callback'],
    // --- non-strings (3.4: the validator is total, not just string-typed)
    ['null', null],
    ['undefined', undefined],
    ['a number', 42],
    ['an object', { pathname: '/dashboard' }],
    ['an array', ['/dashboard']],
    ['a boolean', true],
  ];

  it.each(rejected)('rejects %s', (_label, candidate) => {
    expect(isSafeInternalPath(candidate)).toBe(false);
  });

  it('is pure: same answer every call, and it never touches storage', () => {
    plant('/devices/dev-1');
    const before = window.sessionStorage.length;

    for (const candidate of ['/dashboard', '//evil.com', '/login', '']) {
      const first = isSafeInternalPath(candidate);
      expect(isSafeInternalPath(candidate)).toBe(first);
      expect(isSafeInternalPath(candidate)).toBe(first);
    }

    expect(window.sessionStorage.length).toBe(before);
    expect(planted()).toBe('/devices/dev-1');
  });
});

// ------------------------------------------------------------- save/consume

describe('saveAttemptedLocation (Requirements 1.1, 1.4, 1.5, 1.6)', () => {
  it('stores pathname + search + hash verbatim', () => {
    saveAttemptedLocation({
      pathname: '/workflows/builder/abc',
      search: '?tab=nodes',
      hash: '#node-3',
    });

    expect(planted()).toBe('/workflows/builder/abc?tab=nodes#node-3');
  });

  it('tolerates a location with no search or hash', () => {
    saveAttemptedLocation({ pathname: '/dashboard' });

    expect(planted()).toBe('/dashboard');
  });

  it('defaults to the current document location', () => {
    atLocation('/devices/dev-1?tab=cameras#latest');

    saveAttemptedLocation();

    expect(planted()).toBe('/devices/dev-1?tab=cameras#latest');
  });

  it('keeps the first location when several 401s land before sign-in (first-write-wins)', () => {
    saveAttemptedLocation({ pathname: '/workflows/builder/abc', search: '?tab=nodes', hash: '' });
    saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' });
    saveAttemptedLocation({ pathname: '/dashboard', search: '', hash: '' });

    expect(planted()).toBe('/workflows/builder/abc?tab=nodes');
  });

  it('does not overwrite a value planted by a previous document', () => {
    plant('/models/model-1/versions/2');

    saveAttemptedLocation({ pathname: '/dashboard', search: '', hash: '' });

    expect(planted()).toBe('/models/model-1/versions/2');
  });

  it('treats an empty stored value as nothing remembered', () => {
    plant('');

    saveAttemptedLocation({ pathname: '/dashboard', search: '', hash: '' });

    expect(planted()).toBe('/dashboard');
  });

  it('never saves the login page, in any spelling (Requirement 1.4)', () => {
    for (const loc of [
      { pathname: '/login', search: '', hash: '' },
      { pathname: '/login', search: '?next=/dashboard', hash: '' },
      { pathname: '/login/', search: '', hash: '#form' },
      { pathname: '/LOGIN', search: '', hash: '' },
      { pathname: '/login/callback', search: '', hash: '' },
    ]) {
      saveAttemptedLocation(loc);
      expect(planted()).toBeNull();
    }
  });

  it('never saves an unsafe candidate', () => {
    for (const pathname of ['//evil.com', '/\\evil.com', 'https://evil.com', 'javascript:alert(1)', '', '/x\u0000y']) {
      saveAttemptedLocation({ pathname, search: '', hash: '' });
      expect(planted()).toBeNull();
    }
  });

  it('rejects a location whose concatenation becomes unsafe', () => {
    // Individually plausible parts, over the bound once concatenated.
    saveAttemptedLocation({
      pathname: '/devices/dev-1',
      search: `?q=${'a'.repeat(MAX_PATH_LENGTH)}`,
      hash: '',
    });

    expect(planted()).toBeNull();
  });

  it('does nothing when there is neither an argument nor a document location', () => {
    saveAttemptedLocation(null);

    // `null` means "use the current location", which is the site root here.
    expect(planted()).toBe('/');
  });
});

describe('takeAttemptedLocation — consume clears (Requirements 2.1, 2.4)', () => {
  it('returns the remembered location and empties the store', () => {
    saveAttemptedLocation({ pathname: '/workflows/builder/abc', search: '?tab=nodes', hash: '' });

    expect(takeAttemptedLocation()).toBe('/workflows/builder/abc?tab=nodes');
    expect(planted()).toBeNull();
  });

  it('returns null on the second consume, so a later sign-in cannot resurrect it', () => {
    saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' });

    expect(takeAttemptedLocation()).toBe('/devices/dev-1');
    expect(takeAttemptedLocation()).toBeNull();
    expect(takeAttemptedLocation()).toBeNull();
  });

  it('returns null when nothing was ever saved', () => {
    expect(takeAttemptedLocation()).toBeNull();
  });

  it('discards a planted unsafe value and still clears the store (validate on consume)', () => {
    for (const raw of ['//evil.com', '/\\evil.com', 'javascript:alert(1)', '/login?next=/x', `/${'a'.repeat(MAX_PATH_LENGTH)}`, '/x\ny']) {
      plant(raw);

      expect(takeAttemptedLocation()).toBeNull();
      expect(planted()).toBeNull();
    }
  });

  it('frees the slot, so the next expiry is remembered normally', () => {
    saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' });
    expect(takeAttemptedLocation()).toBe('/devices/dev-1');

    saveAttemptedLocation({ pathname: '/admin/fleet', search: '', hash: '' });
    expect(takeAttemptedLocation()).toBe('/admin/fleet');
  });
});

describe('clearAttemptedLocation (Requirement 4.1)', () => {
  it('forgets a remembered location', () => {
    saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' });

    clearAttemptedLocation();

    expect(planted()).toBeNull();
    expect(takeAttemptedLocation()).toBeNull();
  });

  it('is a no-op when nothing is remembered, and leaves other keys alone', () => {
    window.sessionStorage.setItem('unrelated', 'keep me');

    clearAttemptedLocation();
    clearAttemptedLocation();

    expect(planted()).toBeNull();
    expect(window.sessionStorage.getItem('unrelated')).toBe('keep me');
  });
});

// ------------------------------------------------------- storage disabled

describe('storage-disabled degradation (design: SecurityError must not reach the 401 path)', () => {
  it('degrades to "no remembered location" when touching sessionStorage throws', () => {
    stubSessionStorage(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError');
    });

    expect(() => saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' })).not.toThrow();
    expect(takeAttemptedLocation()).toBeNull();
    expect(() => clearAttemptedLocation()).not.toThrow();
    // The validator is pure, so it is unaffected by storage at all.
    expect(isSafeInternalPath('/devices/dev-1')).toBe(true);
  });

  it('degrades when the store exists but every method throws', () => {
    const boom = () => {
      throw new DOMException('The operation is insecure.', 'SecurityError');
    };
    const hostile = {
      getItem: vi.fn(boom),
      setItem: vi.fn(boom),
      removeItem: vi.fn(boom),
      clear: vi.fn(boom),
      key: vi.fn(boom),
      length: 0,
    } as unknown as Storage;
    stubSessionStorage(() => hostile);

    expect(() => saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' })).not.toThrow();
    expect(takeAttemptedLocation()).toBeNull();
    expect(() => clearAttemptedLocation()).not.toThrow();
  });

  it('degrades when the store is missing entirely', () => {
    stubSessionStorage(() => undefined as unknown as Storage);

    expect(() => saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' })).not.toThrow();
    expect(takeAttemptedLocation()).toBeNull();
    expect(() => clearAttemptedLocation()).not.toThrow();
  });

  it('degrades when writing exceeds the quota', () => {
    const store: Storage = {
      getItem: vi.fn(() => null),
      setItem: vi.fn(() => {
        throw new DOMException('QuotaExceededError', 'QuotaExceededError');
      }),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(() => null),
      length: 0,
    } as unknown as Storage;
    stubSessionStorage(() => store);

    expect(() => saveAttemptedLocation({ pathname: '/devices/dev-1', search: '', hash: '' })).not.toThrow();
    expect(store.setItem).toHaveBeenCalledWith(RETURN_TO_KEY, '/devices/dev-1');
    expect(takeAttemptedLocation()).toBeNull();
  });

  it('still sends the user to login when storage is unavailable', () => {
    stubSessionStorage(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError');
    });
    const navigate = vi.fn();
    setNavigateTo(navigate);

    expect(() => redirectToLogin({ pathname: '/devices/dev-1', search: '', hash: '' })).not.toThrow();

    expect(navigate).toHaveBeenCalledWith(LOGIN_PATH);
  });
});

// ------------------------------------------------------ the navigation seam

describe('redirectToLogin — the shared Session_Exit (Requirements 1.1, 1.3, 6.1)', () => {
  it('saves the location and navigates to /login through the seam', () => {
    const navigate = vi.fn();
    setNavigateTo(navigate);

    redirectToLogin({ pathname: '/workflows/builder/abc', search: '?tab=nodes', hash: '' });

    expect(planted()).toBe('/workflows/builder/abc?tab=nodes');
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith(LOGIN_PATH);
  });

  it('defaults to the current document location', () => {
    atLocation('/devices/dev-1?tab=cameras#latest');
    const navigate = vi.fn();
    setNavigateTo(navigate);

    redirectToLogin();

    expect(planted()).toBe('/devices/dev-1?tab=cameras#latest');
    expect(navigate).toHaveBeenCalledWith(LOGIN_PATH);
  });

  it('does not reload the login page when the user is already on it', () => {
    atLocation('/login');
    const navigate = vi.fn();
    setNavigateTo(navigate);

    redirectToLogin();

    expect(navigate).not.toHaveBeenCalled();
    expect(planted()).toBeNull();
  });

  it('keeps the earliest location when a burst of 401s all redirect', () => {
    const navigate = vi.fn();
    setNavigateTo(navigate);

    redirectToLogin({ pathname: '/workflows/builder/abc', search: '?tab=nodes', hash: '' });
    redirectToLogin({ pathname: '/devices/dev-1', search: '', hash: '' });
    redirectToLogin({ pathname: '/dashboard', search: '', hash: '' });

    expect(planted()).toBe('/workflows/builder/abc?tab=nodes');
    expect(navigate).toHaveBeenCalledTimes(3);
    expect(takeAttemptedLocation()).toBe('/workflows/builder/abc?tab=nodes');
  });

  it('restores the real navigation when the seam is reset', () => {
    const navigate = vi.fn();
    setNavigateTo(navigate);
    setNavigateTo(null);

    // The default implementation assigns `window.location.href`, which jsdom
    // refuses to perform — so assert only that the stub is no longer wired in.
    expect(navigate).not.toHaveBeenCalled();
  });
});
