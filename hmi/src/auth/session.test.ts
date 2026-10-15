import fc from "fast-check";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetApiBase, setApiBase } from "../api/base";
import {
  clearRetainedCredentials,
  clearSession,
  decideStartupScreen,
  getRetainedCredentials,
  loadSession,
  parseStoredSession,
  retainCredentials,
  saveSession,
  SESSION_STORAGE_KEY,
  startupScreen,
  type SessionStorageLike,
  type StoredSession,
} from "./session";

// Unit tests for session management (design Decision 4).
// _Requirements: 1.1, 1.2, 1.5_

/** The LocalServer that issued the session under test, and another one. */
const HERE = "http://192.168.8.224:5000";
const ELSEWHERE = "http://192.168.8.225:5000";

afterEach(() => {
  resetApiBase();
});

/** Minimal in-memory implementation of the Storage subset the module uses. */
function makeStorage(initial: Record<string, string> = {}): SessionStorageLike & {
  data: Map<string, string>;
} {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

/**
 * Property test for the startup screen decision, shared by both kiosk entries
 * (`/hmi/index.html` and `/hmi/triple.html`).
 *
 * **Feature: quality-station-hmi, Property 1: Startup session decision**
 *
 * **Feature: imts-triple-inspection-hmi, Property 1: Startup session decision**
 *
 * **Validates: Requirements 1.1, 1.5**
 *
 * The Triple_HMI reuses `auth/session.ts` unchanged and resumes from the same
 * `localStorage["hmi.session"]` namespace (design "Session storage ... default
 * is the shared key"), so the storage-key composition is exercised here for
 * both entries with no separate key generator.
 */

/** Epoch-second instants, including 0 and negatives, around a realistic clock. */
const epochSeconds = fc.oneof(
  fc.integer({ min: -10_000, max: 10_000 }),
  fc.integer({ min: 1_700_000_000, max: 1_900_000_000 }),
);

/**
 * Stored session state: absent, or a token with any `expiresAt`, issued by
 * this LocalServer or by another one.
 */
const storedSession: fc.Arbitrary<StoredSession | null> = fc.oneof(
  { arbitrary: fc.constant(null), weight: 1 },
  {
    arbitrary: fc.record({
      token: fc.string({ minLength: 1 }),
      expiresAt: epochSeconds,
      origin: fc.constantFrom(HERE, ELSEWHERE),
    }),
    weight: 3,
  },
);

describe("Property 1: Startup session decision", () => {
  it("is 'login' iff no token is stored or expiresAt <= now", () => {
    fc.assert(
      fc.property(storedSession, epochSeconds, (session, now) => {
        const decision = decideStartupScreen(session, now);
        const mustLogin = session === null || session.expiresAt <= now;
        expect(decision).toBe(mustLogin ? "login" : "app");
      }),
    );
  });

  it("holds through the shared hmi.session storage key both entries read", () => {
    fc.assert(
      fc.property(storedSession, epochSeconds, (session, now) => {
        const storage = makeStorage();
        if (session !== null) saveSession(session, storage);
        // Both the single-inspection and triple entries resume through this
        // one key, so the persisted round trip decides identically — for the
        // LocalServer that issued the session. For any other one the kiosk
        // is signed out, so the login form is shown.
        expect(storage.data.has(SESSION_STORAGE_KEY)).toBe(session !== null);
        const usable = session !== null && session.origin === HERE ? session : null;
        expect(startupScreen(storage, now, HERE)).toBe(
          decideStartupScreen(usable, now),
        );
      }),
    );
  });
});

describe("decideStartupScreen", () => {
  const now = 1_736_950_000; // epoch seconds

  it("requires login when no session is stored (1.1)", () => {
    expect(decideStartupScreen(null, now)).toBe("login");
  });

  it("requires login when the token is expired (1.1)", () => {
    expect(decideStartupScreen({ token: "t", expiresAt: now - 1 }, now)).toBe(
      "login",
    );
  });

  it("requires login when expiresAt equals the current time (1.1: at or before)", () => {
    expect(decideStartupScreen({ token: "t", expiresAt: now }, now)).toBe(
      "login",
    );
  });

  it("resumes without prompting when the token is still valid (1.5)", () => {
    expect(decideStartupScreen({ token: "t", expiresAt: now + 1 }, now)).toBe(
      "app",
    );
  });
});

describe("parseStoredSession", () => {
  it("round-trips a valid payload", () => {
    expect(
      parseStoredSession(`{"token":"abc","expiresAt":123,"origin":"${HERE}"}`),
    ).toEqual({ token: "abc", expiresAt: 123, origin: HERE });
  });

  it("treats absent, corrupt, and wrongly-shaped payloads as no session", () => {
    expect(parseStoredSession(null)).toBeNull();
    expect(parseStoredSession("not json")).toBeNull();
    expect(parseStoredSession('"just a string"')).toBeNull();
    expect(parseStoredSession("[1,2]")).toBeNull();
    expect(parseStoredSession('{"token":"","expiresAt":123,"origin":""}')).toBeNull();
    expect(parseStoredSession('{"token":"abc","origin":""}')).toBeNull();
    expect(parseStoredSession('{"expiresAt":123,"origin":""}')).toBeNull();
    expect(parseStoredSession('{"token":"abc","expiresAt":"123","origin":""}')).toBeNull();
    expect(parseStoredSession('{"token":"abc","expiresAt":123,"origin":5000}')).toBeNull();
  });

  it("drops a legacy payload that does not record the issuing origin", () => {
    // Stored before sessions were origin-bound: nothing says which LocalServer
    // issued the token, so it is never sent anywhere (one sign-in after update).
    expect(parseStoredSession('{"token":"abc","expiresAt":123}')).toBeNull();
  });
});

describe("session persistence in localStorage['hmi.session'] (1.2)", () => {
  it("saves token + expiresAt + the issuing origin under the hmi.session key", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-1", expiresAt: 1_736_950_000, origin: HERE }, storage);
    expect(storage.data.get(SESSION_STORAGE_KEY)).toBe(
      `{"token":"tok-1","expiresAt":1736950000,"origin":"${HERE}"}`,
    );
  });

  it("binds a session to the current API origin unless told otherwise", () => {
    setApiBase(HERE);
    const storage = makeStorage();
    saveSession({ token: "tok-1", expiresAt: 9e9 }, storage);
    expect(parseStoredSession(storage.data.get(SESSION_STORAGE_KEY) ?? null)?.origin).toBe(HERE);
    expect(loadSession(storage)?.token).toBe("tok-1");
  });

  it("loads what it saved", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-2", expiresAt: 42, origin: HERE }, storage);
    expect(loadSession(storage, HERE)).toEqual({ token: "tok-2", expiresAt: 42, origin: HERE });
  });

  it("returns null for an empty or corrupted store", () => {
    expect(loadSession(makeStorage())).toBeNull();
    expect(loadSession(makeStorage({ [SESSION_STORAGE_KEY]: "@#$%" }))).toBeNull();
    expect(loadSession(null)).toBeNull();
  });

  it("clearSession discards the stored session", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-3", expiresAt: 99 }, storage);
    clearSession(storage);
    expect(loadSession(storage)).toBeNull();
  });

  it("does not throw when the underlying storage throws", () => {
    const throwing: SessionStorageLike = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    expect(loadSession(throwing)).toBeNull();
    expect(() => saveSession({ token: "t", expiresAt: 1 }, throwing)).not.toThrow();
    expect(() => clearSession(throwing)).not.toThrow();
  });
});

describe("startupScreen (storage + clock composition)", () => {
  const now = 1_736_950_000;

  it("login with an empty store (1.1)", () => {
    expect(startupScreen(makeStorage(), now)).toBe("login");
  });

  it("login with an expired stored session (1.1)", () => {
    const storage = makeStorage();
    saveSession({ token: "t", expiresAt: now }, storage);
    expect(startupScreen(storage, now)).toBe("login");
  });

  it("resumes with a valid stored session (1.5)", () => {
    const storage = makeStorage();
    saveSession({ token: "t", expiresAt: now + 3600 }, storage);
    expect(startupScreen(storage, now)).toBe("app");
  });

  it("login with a corrupted stored session", () => {
    expect(
      startupScreen(makeStorage({ [SESSION_STORAGE_KEY]: "{broken" }), now),
    ).toBe("login");
  });
});

describe("origin binding", () => {
  const now = 1_736_950_000;
  const LEGACY = JSON.stringify({ token: "legacy-tok", expiresAt: now + 3600 });

  it("returns the session only to the origin that issued it", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-A", expiresAt: now + 3600, origin: HERE }, storage);
    expect(loadSession(storage, HERE)?.token).toBe("tok-A");
    expect(loadSession(storage, ELSEWHERE)).toBeNull();
    expect(loadSession(storage, "")).toBeNull();
  });

  it("is signed out (login form) for any other origin", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-A", expiresAt: now + 3600, origin: HERE }, storage);
    expect(startupScreen(storage, now, HERE)).toBe("app");
    expect(startupScreen(storage, now, ELSEWHERE)).toBe("login");
  });

  it("follows the current API origin by default", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-A", expiresAt: now + 3600, origin: HERE }, storage);
    setApiBase(HERE);
    expect(loadSession(storage)?.token).toBe("tok-A");
    expect(startupScreen(storage, now)).toBe("app");
    setApiBase(ELSEWHERE);
    expect(loadSession(storage)).toBeNull();
    expect(startupScreen(storage, now)).toBe("login");
  });

  it("drops a legacy session with no origin: login form everywhere", () => {
    const storage = makeStorage({ [SESSION_STORAGE_KEY]: LEGACY });
    expect(loadSession(storage, HERE)).toBeNull();
    expect(loadSession(storage, "")).toBeNull();
    expect(startupScreen(storage, now, HERE)).toBe("login");
  });

  it("clearSession keeps a session another origin issued", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-A", expiresAt: now + 3600, origin: HERE }, storage);
    clearSession(storage, ELSEWHERE);
    expect(loadSession(storage, HERE)?.token).toBe("tok-A");
    clearSession(storage, HERE);
    expect(storage.data.has(SESSION_STORAGE_KEY)).toBe(false);
  });

  it("clearSession removes a legacy or corrupt entry", () => {
    const legacy = makeStorage({ [SESSION_STORAGE_KEY]: LEGACY });
    clearSession(legacy, HERE);
    expect(legacy.data.has(SESSION_STORAGE_KEY)).toBe(false);
    const corrupt = makeStorage({ [SESSION_STORAGE_KEY]: "{broken" });
    clearSession(corrupt, HERE);
    expect(corrupt.data.has(SESSION_STORAGE_KEY)).toBe(false);
  });
});

describe("in-memory credential retention", () => {
  beforeEach(() => clearRetainedCredentials());

  it("retains and returns the most recent credentials", () => {
    retainCredentials({ username: "op", password: "pw" });
    expect(getRetainedCredentials()).toEqual({ username: "op", password: "pw" });
    retainCredentials({ username: "op2", password: "pw2" });
    expect(getRetainedCredentials()).toEqual({ username: "op2", password: "pw2" });
  });

  it("returns null when nothing is retained or after clearing", () => {
    expect(getRetainedCredentials()).toBeNull();
    retainCredentials({ username: "op", password: "pw" });
    clearRetainedCredentials();
    expect(getRetainedCredentials()).toBeNull();
  });

  it("never writes credentials to the session storage", () => {
    const storage = makeStorage();
    retainCredentials({ username: "op", password: "secret" });
    saveSession({ token: "tok", expiresAt: 1 }, storage);
    const persisted = [...storage.data.values()].join("");
    expect(persisted).not.toContain("secret");
    expect(persisted).not.toContain("op");
  });

  it("returns a copy so callers cannot mutate the retained credentials", () => {
    retainCredentials({ username: "op", password: "pw" });
    const first = getRetainedCredentials();
    first!.password = "mutated";
    expect(getRetainedCredentials()).toEqual({ username: "op", password: "pw" });
  });

  it("returns them only for the origin that accepted them", () => {
    retainCredentials({ username: "op", password: "pw" }, HERE);
    expect(getRetainedCredentials(HERE)).toEqual({ username: "op", password: "pw" });
    expect(getRetainedCredentials(ELSEWHERE)).toBeNull();
  });
});
