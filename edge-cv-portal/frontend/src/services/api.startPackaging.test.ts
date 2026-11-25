/**
 * `apiService.startPackaging` request body (rfdetr-seg-support task 12.1;
 * Requirement 6.4 carrier, C31).
 *
 * The POST body is `{targets, auto_triggered}` for every caller that exists
 * today (CompilationTab, SmartImport, the vLLM publish controller) and gains
 * `output_mode` only when the new `options.outputMode` is given — the
 * Output_Mode override the Seg record's Package action sends (task 12.3).
 *
 * `fetch` is stubbed as `api.debug.test.ts` does; Amplify's refresh is
 * mocked so a 401 could never reach it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { fetchAuthSessionMock } = vi.hoisted(() => ({ fetchAuthSessionMock: vi.fn() }));
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: fetchAuthSessionMock }));

import { apiService } from './api';

const PACKAGED = {
  training_id: 'tj-1',
  packaged_components: [{ target: 'arm64_jp6', status: 'Completed' }],
  message: 'ok',
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

type FetchMock = ReturnType<typeof vi.fn<(url: string, init?: RequestInit) => Promise<Response>>>;

function stubFetch(): FetchMock {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => jsonResponse(200, PACKAGED));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function sentBody(fetchMock: FetchMock): string {
  const init = fetchMock.mock.calls[0][1] ?? {};
  return String(init.body);
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.setItem('idToken', 'token-1');
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('startPackaging body', () => {
  it('POSTs /training/<id>/package with an empty body when nothing is given', async () => {
    const fetchMock = stubFetch();
    await expect(apiService.startPackaging('tj-1')).resolves.toEqual(PACKAGED);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = new URL(fetchMock.mock.calls[0][0]);
    expect(url.pathname.endsWith('/training/tj-1/package')).toBe(true);
    const init = fetchMock.mock.calls[0][1] ?? {};
    expect(init.method).toBe('POST');
    // JSON.stringify drops the undefined targets / auto_triggered exactly as before.
    expect(sentBody(fetchMock)).toBe('{}');
  });

  it('keeps the byte-identical {targets, auto_triggered} body for the existing callers', async () => {
    const fetchMock = stubFetch();
    await apiService.startPackaging('tj-1', ['arm64_jp6', 'amd64'], true);
    expect(sentBody(fetchMock)).toBe(JSON.stringify({ targets: ['arm64_jp6', 'amd64'], auto_triggered: true }));
  });

  it('a signal-only options object (the vLLM publish controller) adds nothing to the body', async () => {
    const fetchMock = stubFetch();
    const controller = new AbortController();
    await apiService.startPackaging('tj-1', undefined, true, { signal: controller.signal });
    expect(sentBody(fetchMock)).toBe(JSON.stringify({ auto_triggered: true }));
    expect(JSON.parse(sentBody(fetchMock))).not.toHaveProperty('output_mode');
    const init = fetchMock.mock.calls[0][1] ?? {};
    expect(init.signal).toBe(controller.signal);
  });

  it('appends output_mode only when outputMode is given (the Seg Package override)', async () => {
    const fetchMock = stubFetch();
    await apiService.startPackaging('tj-1', undefined, undefined, { outputMode: 'semantic' });
    expect(JSON.parse(sentBody(fetchMock))).toEqual({ output_mode: 'semantic' });

    fetchMock.mockClear();
    await apiService.startPackaging('tj-1', ['arm64_jp6'], false, { outputMode: 'instance' });
    expect(JSON.parse(sentBody(fetchMock))).toEqual({
      targets: ['arm64_jp6'],
      auto_triggered: false,
      output_mode: 'instance',
    });
  });
});
