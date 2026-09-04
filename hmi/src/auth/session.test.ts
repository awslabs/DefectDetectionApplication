import fc from "fast-check";
import { beforeEach, describe, expect, it } from "vitest";
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

/** Stored session state: absent, or a token with any `expiresAt`. */
const storedSession: fc.Arbitrary<StoredSession | null> = fc.oneof(
  { arbitrary: fc.constant(null), weight: 1 },
  {
    arbitrary: fc.record({
      token: fc.string({ minLength: 1 }),
      expiresAt: epochSeconds,
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
        // one key, so the persisted round trip decides identically.
        expect(storage.data.has(SESSION_STORAGE_KEY)).toBe(session !== null);
        expect(startupScreen(storage, now)).toBe(
          decideStartupScreen(session, now),
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
    expect(parseStoredSession('{"token":"abc","expiresAt":123}')).toEqual({
      token: "abc",
      expiresAt: 123,
    });
  });

  it("treats absent, corrupt, and wrongly-shaped payloads as no session", () => {
    expect(parseStoredSession(null)).toBeNull();
    expect(parseStoredSession("not json")).toBeNull();
    expect(parseStoredSession('"just a string"')).toBeNull();
    expect(parseStoredSession("[1,2]")).toBeNull();
    expect(parseStoredSession('{"token":"","expiresAt":123}')).toBeNull();
    expect(parseStoredSession('{"token":"abc"}')).toBeNull();
    expect(parseStoredSession('{"expiresAt":123}')).toBeNull();
    expect(parseStoredSession('{"token":"abc","expiresAt":"123"}')).toBeNull();
  });
});

describe("session persistence in localStorage['hmi.session'] (1.2)", () => {
  it("saves token + expiresAt under the hmi.session key", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-1", expiresAt: 1_736_950_000 }, storage);
    expect(storage.data.get(SESSION_STORAGE_KEY)).toBe(
      '{"token":"tok-1","expiresAt":1736950000}',
    );
  });

  it("loads what it saved", () => {
    const storage = makeStorage();
    saveSession({ token: "tok-2", expiresAt: 42 }, storage);
    expect(loadSession(storage)).toEqual({ token: "tok-2", expiresAt: 42 });
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
});
