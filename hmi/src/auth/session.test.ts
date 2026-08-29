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
