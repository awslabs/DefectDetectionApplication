/**
 * Unit tests for the workflow telemetry dashboards API client methods
 * (workflow-telemetry-dashboards task 5.1, Requirements 17.1, 19.4).
 *
 * - `getDashboardSeries` serializes `from`/`to` and only adds `device_id`
 *   and `measures` when given (design §8.3: the pages' one request per
 *   Dashboard per refresh carries neither);
 * - `getLiveFrame` passes the previous answer's `frame_token` as `have` and
 *   omits it on the first poll (design §9.3);
 * - `putTelemetrySettings` reports the 202 of the pending shadow writes;
 * - refusals arrive as `TelemetryApiError` with `errorCode`, `remedy` and
 *   `field`; the routes, methods and bodies of the other calls.
 *
 * `fetch` is stubbed as `api.debug.test.ts` does; Amplify's refresh is mocked
 * so a 401 could never reach it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { fetchAuthSessionMock } = vi.hoisted(() => ({ fetchAuthSessionMock: vi.fn() }));
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: fetchAuthSessionMock }));

import { TelemetryApiError, apiService, isTelemetryApiError } from './api';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function stubFetch(status: number, body: unknown): FetchMock {
  const fetchMock: FetchMock = vi.fn(async (..._args: unknown[]) => jsonResponse(status, body));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

type FetchMock = ReturnType<typeof vi.fn<(...args: unknown[]) => Promise<Response>>>;

function requestedUrl(fetchMock: FetchMock, call = 0): URL {
  return new URL(String(fetchMock.mock.calls[call][0]));
}

function requestInit(fetchMock: FetchMock, call = 0): RequestInit {
  return (fetchMock.mock.calls[call][1] ?? {}) as RequestInit;
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.setItem('idToken', 'token-1');
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('getDashboardSeries query serialization (design §8.3)', () => {
  const answer = { resolution: 'minute', devices: {}, from: 1000, to: 2000, updatedAt: 2000, refreshSeconds: 60 };

  it('sends only from and to when the optional parameters are absent (the pages\' one request)', async () => {
    const fetchMock = stubFetch(200, answer);

    await apiService.getDashboardSeries('dash-1', { from: 1000, to: 2000 });

    const url = requestedUrl(fetchMock);
    expect(url.pathname.endsWith('/telemetry/dashboards/dash-1/series')).toBe(true);
    expect(Object.fromEntries(url.searchParams.entries())).toEqual({ from: '1000', to: '2000' });
    expect(url.searchParams.has('device_id')).toBe(false);
    expect(url.searchParams.has('measures')).toBe(false);
    expect(requestInit(fetchMock).method ?? 'GET').toBe('GET');
  });

  it('adds device_id and the comma-separated measures when given', async () => {
    const fetchMock = stubFetch(200, answer);

    await apiService.getDashboardSeries('dash-1', {
      from: 1000,
      to: 2000,
      device_id: 'thing-a',
      measures: ['runs_completed', 'latency_p95_ms'],
    });

    const url = requestedUrl(fetchMock);
    expect(Object.fromEntries(url.searchParams.entries())).toEqual({
      from: '1000',
      to: '2000',
      device_id: 'thing-a',
      measures: 'runs_completed,latency_p95_ms',
    });
  });

  it('truncates fractional milliseconds and drops an empty measures list', async () => {
    const fetchMock = stubFetch(200, answer);

    await apiService.getDashboardSeries('dash-1', { from: 1000.9, to: 2000.2, measures: [] });

    const url = requestedUrl(fetchMock);
    expect(url.searchParams.get('from')).toBe('1000');
    expect(url.searchParams.get('to')).toBe('2000');
    expect(url.searchParams.has('measures')).toBe(false);
  });

  it('surfaces 400 range_too_long as a TelemetryApiError with max_days', async () => {
    stubFetch(400, { error: 'The range may span at most 400 days', error_code: 'range_too_long', max_days: 400 });

    const caught = await apiService.getDashboardSeries('dash-1', { from: 0, to: 1 }).catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(true);
    expect((caught as TelemetryApiError).status).toBe(400);
    expect((caught as TelemetryApiError).errorCode).toBe('range_too_long');
    expect((caught as TelemetryApiError).body.max_days).toBe(400);
  });
});

describe('getLiveFrame and the have token (design §9.3)', () => {
  const frameAnswer = { frame: null, frame_token: 'inst:7', age_s: 1 };

  it('passes the previous answer\'s frame_token as have', async () => {
    const fetchMock = stubFetch(200, frameAnswer);

    await apiService.getLiveFrame('dash-1', 'thing-a', '0f1e2d3c-4b5a-4968-8776-65544332211a:41');

    const url = requestedUrl(fetchMock);
    expect(url.pathname.endsWith('/telemetry/dashboards/dash-1/live')).toBe(true);
    expect(Object.fromEntries(url.searchParams.entries())).toEqual({
      device_id: 'thing-a',
      have: '0f1e2d3c-4b5a-4968-8776-65544332211a:41',
    });
  });

  it('omits have on the first poll (undefined or null)', async () => {
    const fetchMock = stubFetch(200, frameAnswer);

    await apiService.getLiveFrame('dash-1', 'thing-a');
    await apiService.getLiveFrame('dash-1', 'thing-a', null);

    expect([...requestedUrl(fetchMock, 0).searchParams.keys()]).toEqual(['device_id']);
    expect([...requestedUrl(fetchMock, 1).searchParams.keys()]).toEqual(['device_id']);
  });

  it('URL-encodes a device id with a colon', async () => {
    const fetchMock = stubFetch(200, frameAnswer);

    await apiService.getLiveFrame('dash-1', 'thing:a', 'inst:1');

    expect(requestedUrl(fetchMock).searchParams.get('device_id')).toBe('thing:a');
    expect(String(fetchMock.mock.calls[0][0])).toContain('device_id=thing%3Aa');
  });
});

describe('acquireLiveLease', () => {
  it('posts the device id to the live-lease route', async () => {
    const fetchMock = stubFetch(200, {
      device_id: 'thing-a', workflow_id: 'wf-1', slot: 0, startedAt: 1, leaseExpiresAt: 2, expiresAt: 2,
      renewSeconds: 20, region: 'us-east-1', region_source: 'header',
    });

    const lease = await apiService.acquireLiveLease('dash-1', 'thing-a');

    const init = requestInit(fetchMock);
    expect(requestedUrl(fetchMock).pathname.endsWith('/telemetry/dashboards/dash-1/live-lease')).toBe(true);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ device_id: 'thing-a' });
    expect(lease.renewSeconds).toBe(20);
  });

  it('keeps the reason of a 429 live_capacity_reached', async () => {
    stubFetch(429, {
      error: 'No live slot is available', error_code: 'live_capacity_reached', reason: 'device_live_limit', max: 8,
      remedy: "Wait for another viewer's lease to lapse.",
    });

    const caught = await apiService.acquireLiveLease('dash-1', 'thing-a').catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(true);
    expect((caught as TelemetryApiError).status).toBe(429);
    expect((caught as TelemetryApiError).errorCode).toBe('live_capacity_reached');
    expect((caught as TelemetryApiError).reason).toBe('device_live_limit');
    expect((caught as TelemetryApiError).remedy).toContain('lease to lapse');
  });

  it('flags a 409 registry_enforcement_required', async () => {
    stubFetch(409, {
      error: 'Registry enforcement is off', error_code: 'registry_enforcement_required',
      remedy: 'Turn on registry enforcement (PORTAL_REGISTRY_ENFORCED) once the registry backfill is complete',
    });

    const caught = await apiService.acquireLiveLease('dash-1', 'thing-a').catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(true);
    expect((caught as TelemetryApiError).isEnforcementRequired).toBe(true);
  });
});

describe('settings', () => {
  it('GET reads the use case\'s settings and checklist', async () => {
    const fetchMock = stubFetch(200, { settings: { enabled: false }, checklist: [] });

    await apiService.getTelemetrySettings('uc-1');

    expect(requestedUrl(fetchMock).pathname.endsWith('/telemetry/usecases/uc-1/settings')).toBe(true);
    expect(requestInit(fetchMock).method ?? 'GET').toBe('GET');
  });

  it('PUT sends the body and reports a 200', async () => {
    const fetchMock = stubFetch(200, { settings: { enabled: true }, checklist: [], devices: [], unrecorded_devices: [] });

    const answer = await apiService.putTelemetrySettings('uc-1', { enabled: true, summaryIntervalS: 10, idleHeartbeatS: 60 });

    const init = requestInit(fetchMock);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual({ enabled: true, summaryIntervalS: 10, idleHeartbeatS: 60 });
    expect(answer.status).toBe(200);
  });

  it('PUT reports the 202 of the pending shadow writes', async () => {
    stubFetch(202, {
      settings: { enabled: true }, checklist: [], devices: [], unrecorded_devices: [], pending_devices: 41,
      message: 'The device shadows are written by the maintenance function within 5 minutes',
    });

    const answer = await apiService.putTelemetrySettings('uc-1', { enabled: true });

    expect(answer.status).toBe(202);
    expect(answer.pending_devices).toBe(41);
  });

  it('a 400 invalid_settings carries the field', async () => {
    stubFetch(400, { error: 'summaryIntervalS must be between 5 and 60', error_code: 'invalid_settings', field: 'summaryIntervalS' });

    const caught = await apiService.putTelemetrySettings('uc-1', { enabled: true, summaryIntervalS: 3 }).catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(true);
    expect((caught as TelemetryApiError).errorCode).toBe('invalid_settings');
    expect((caught as TelemetryApiError).field).toBe('summaryIntervalS');
    expect((caught as TelemetryApiError).message).toContain('between 5 and 60');
  });
});

describe('templates, rejections and schemas', () => {
  it('getTelemetryTemplates serializes workflow_id and the device_ids list', async () => {
    const fetchMock = stubFetch(200, { usecase_id: 'uc-1', templates: [] });

    await apiService.getTelemetryTemplates('uc-1', { workflow_id: 'wf-1', device_ids: ['thing-a', 'thing-b'] });

    const url = requestedUrl(fetchMock);
    expect(url.pathname.endsWith('/telemetry/usecases/uc-1/templates')).toBe(true);
    expect(Object.fromEntries(url.searchParams.entries())).toEqual({ workflow_id: 'wf-1', device_ids: 'thing-a,thing-b' });
  });

  it('getTelemetryTemplates without parameters sends none', async () => {
    const fetchMock = stubFetch(200, { usecase_id: 'uc-1', templates: [] });

    await apiService.getTelemetryTemplates('uc-1');

    expect(requestedUrl(fetchMock).search).toBe('');
  });

  it('getTelemetryRejections reads the use case\'s or the account-wide route', async () => {
    const fetchMock = stubFetch(200, { counts: {}, minutes: [], samples: [], note: '' });

    await apiService.getTelemetryRejections({ usecaseId: 'uc-1' });
    await apiService.getTelemetryRejections({ accountId: '111111111111' });

    expect(requestedUrl(fetchMock, 0).pathname.endsWith('/telemetry/usecases/uc-1/rejections')).toBe(true);
    expect(requestedUrl(fetchMock, 1).pathname.endsWith('/telemetry/rejections')).toBe(true);
    expect(requestedUrl(fetchMock, 1).searchParams.get('account_id')).toBe('111111111111');
  });

  it('getTelemetrySchemas reads /telemetry/schemas', async () => {
    const fetchMock = stubFetch(200, { telemetryNowPollSeconds: 5 });

    const answer = await apiService.getTelemetrySchemas();

    expect(requestedUrl(fetchMock).pathname.endsWith('/telemetry/schemas')).toBe(true);
    expect(answer.telemetryNowPollSeconds).toBe(5);
  });
});

describe('dashboards CRUD and the tile reads', () => {
  it('listDashboards passes usecase_id', async () => {
    const fetchMock = stubFetch(200, { usecase_id: 'uc-1', dashboards: [] });

    await apiService.listDashboards('uc-1');

    const url = requestedUrl(fetchMock);
    expect(url.pathname.endsWith('/telemetry/dashboards')).toBe(true);
    expect(url.searchParams.get('usecase_id')).toBe('uc-1');
  });

  it('createDashboard posts the body', async () => {
    const fetchMock = stubFetch(201, { dashboard: { dashboard_id: 'dash-1' }, devices: [] });
    const body = { usecase_id: 'uc-1', name: 'Line 1', workflow_id: 'wf-1', device_ids: ['thing-a'], template_id: 'generic-detector' };

    await apiService.createDashboard(body);

    const init = requestInit(fetchMock);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual(body);
  });

  it('createDashboard keeps the Device_Record_Remedy of a 400 device_not_recorded', async () => {
    stubFetch(400, {
      error: 'thing-b has no device record for this use case', error_code: 'device_not_recorded', field: 'device_ids',
      remedy: 'A UseCaseAdmin or PortalAdmin sets the device\'s Target architecture on its device page',
    });

    const caught = await apiService
      .createDashboard({ usecase_id: 'uc-1', name: 'x', workflow_id: 'wf-1', device_ids: ['thing-b'], template_id: 'anomaly' })
      .catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(true);
    expect((caught as TelemetryApiError).errorCode).toBe('device_not_recorded');
    expect((caught as TelemetryApiError).remedy).toContain('Target architecture');
  });

  it('get, patch and delete address /telemetry/dashboards/{id}', async () => {
    const fetchMock = stubFetch(200, { dashboard_id: 'dash-1', deleted: true });

    await apiService.getDashboard('dash-1');
    await apiService.patchDashboard('dash-1', { name: 'Renamed' });
    await apiService.deleteDashboard('dash-1');

    for (const call of [0, 1, 2]) {
      expect(requestedUrl(fetchMock, call).pathname.endsWith('/telemetry/dashboards/dash-1')).toBe(true);
    }
    expect(requestInit(fetchMock, 0).method ?? 'GET').toBe('GET');
    expect(requestInit(fetchMock, 1).method).toBe('PATCH');
    expect(JSON.parse(String(requestInit(fetchMock, 1).body))).toEqual({ name: 'Renamed' });
    expect(requestInit(fetchMock, 2).method).toBe('DELETE');
  });

  it('now, events, clips and clip media address their sub-routes', async () => {
    const fetchMock = stubFetch(200, {});

    await apiService.getDashboardNow('dash-1');
    await apiService.getDashboardEvents('dash-1', { from: 1, to: 2, type: 'clip_ready', device_id: 'thing-a', cursor: 'c1' });
    await apiService.getDashboardEvents('dash-1');
    await apiService.getDashboardClips('dash-1', { reason: 'no-helmet', cursor: 'c2' });
    await apiService.getClipMedia('dash-1', 'MTIz');

    expect(requestedUrl(fetchMock, 0).pathname.endsWith('/telemetry/dashboards/dash-1/now')).toBe(true);
    const events = requestedUrl(fetchMock, 1);
    expect(events.pathname.endsWith('/telemetry/dashboards/dash-1/events')).toBe(true);
    expect(Object.fromEntries(events.searchParams.entries())).toEqual({
      from: '1', to: '2', type: 'clip_ready', device_id: 'thing-a', cursor: 'c1',
    });
    expect(requestedUrl(fetchMock, 2).search).toBe('');
    const clips = requestedUrl(fetchMock, 3);
    expect(clips.pathname.endsWith('/telemetry/dashboards/dash-1/clips')).toBe(true);
    expect(Object.fromEntries(clips.searchParams.entries())).toEqual({ reason: 'no-helmet', cursor: 'c2' });
    expect(requestedUrl(fetchMock, 4).pathname.endsWith('/telemetry/dashboards/dash-1/clips/MTIz/media')).toBe(true);
  });

  it('a 403 keeps the Portal-wide INSUFFICIENT_PERMISSIONS code', async () => {
    stubFetch(403, { error: 'Insufficient permissions', error_code: 'INSUFFICIENT_PERMISSIONS' });

    const caught = await apiService.getDashboardNow('dash-1').catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(true);
    expect((caught as TelemetryApiError).status).toBe(403);
    expect((caught as TelemetryApiError).errorCode).toBe('INSUFFICIENT_PERMISSIONS');
  });

  it('a network failure is not wrapped', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));

    const caught = await apiService.getDashboardNow('dash-1').catch((e) => e);

    expect(isTelemetryApiError(caught)).toBe(false);
    expect((caught as Error).message).toBe('Failed to fetch');
  });
});
