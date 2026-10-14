/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
/**
 * Local_Session_Token storage helpers for the LoginGate
 * (portal-user-manager, Requirements 8.1, 8.8, 9.2; design D8).
 *
 * The token issued by `POST /local-auth/login` is kept in `sessionStorage`
 * together with its expiry so the gate can decide, without a round trip,
 * whether an unexpired session exists. The token is attached to every API
 * call through the axios default `Authorization` header.
 */
import axios from "axios";
import { LocalLoginResponse } from "api/LocalAuthAPI";

export const LOCAL_SESSION_STORAGE_KEY = "dda-local-session";

/** The Local_Session_Token material persisted in sessionStorage. */
export interface LocalSession {
  token: string;
  /** Epoch seconds at which the token expires (issuance + 12 h). */
  expiresAt: number;
  role: string;
  username: string;
}

/** Current time in epoch seconds. */
export function nowInSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

/**
 * Pure predicate: a session is usable only while its expiry is strictly in
 * the future (mirrors the server-side `exp <= now` rejection, 8.7).
 */
export function isSessionUnexpired(
  session: LocalSession,
  nowSeconds: number,
): boolean {
  return session.expiresAt > nowSeconds;
}

function isLocalSessionShape(value: unknown): value is LocalSession {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.token === "string" &&
    candidate.token.length > 0 &&
    typeof candidate.expiresAt === "number" &&
    Number.isFinite(candidate.expiresAt) &&
    typeof candidate.role === "string" &&
    typeof candidate.username === "string"
  );
}

/**
 * Load the stored session, returning null (and clearing the stale entry)
 * when it is missing, malformed, or expired.
 */
export function loadStoredSession(
  nowSeconds: number = nowInSeconds(),
): LocalSession | null {
  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(LOCAL_SESSION_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (isLocalSessionShape(parsed) && isSessionUnexpired(parsed, nowSeconds)) {
      return parsed;
    }
  } catch {
    // fall through to cleanup
  }
  clearStoredSession();
  return null;
}

/** Persist a successful login response as the current session. */
export function storeSession(response: LocalLoginResponse): LocalSession {
  const session: LocalSession = {
    token: response.token,
    expiresAt: response.expiresAt,
    role: response.role,
    username: response.username,
  };
  try {
    window.sessionStorage.setItem(
      LOCAL_SESSION_STORAGE_KEY,
      JSON.stringify(session),
    );
  } catch {
    // Storage unavailable: the in-memory session still works for this page.
  }
  return session;
}

/** Remove any persisted session. */
export function clearStoredSession(): void {
  try {
    window.sessionStorage.removeItem(LOCAL_SESSION_STORAGE_KEY);
  } catch {
    // ignore storage errors
  }
}

/**
 * Attach the Local_Session_Token as `Authorization: Bearer` on all API
 * calls (axios default header, the same mechanism the existing token auth
 * uses in authHook.tsx).
 */
export function setLocalSessionAuthHeader(token: string): void {
  axios.defaults.headers.common["Authorization"] = `Bearer ${token}`;
}

/**
 * Clear the default `Authorization` header, but only when it still carries
 * the given Local_Session_Token — never clobber a header installed by the
 * existing bearer-token auth flow (Requirement 10 retention).
 */
export function clearLocalSessionAuthHeader(token: string): void {
  if (axios.defaults.headers.common["Authorization"] === `Bearer ${token}`) {
    delete axios.defaults.headers.common["Authorization"];
  }
}
