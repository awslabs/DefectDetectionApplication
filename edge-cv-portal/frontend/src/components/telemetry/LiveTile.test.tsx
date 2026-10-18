/**
 * Unit tests for the live tile (workflow-telemetry-dashboards task 5.4,
 * Requirements 10.1, 10.5, 10.6, 17.5; design §9.3, §11.3).
 *
 * With fake timers and planned lease / frame fetchers:
 * - the lease on show and every 20 s (the answer's `renewSeconds`);
 * - `getLiveFrame` every 2 s passing the previous answer's `frame_token` as
 *   `have` (absent on the first poll);
 * - the image is kept on `frame: null` and swapped on a new token; a mocked
 *   restart answering a token with a new `instance` and `seq` 0 shows the new
 *   frame on the next poll;
 * - each state: no live source, starting ("the device is not sending" after
 *   15 s), live, stale, paused (after `live_viewing_limit`, with resume),
 *   unavailable with its reason;
 * - the notices for `live_capacity_reached`, `telemetry_disabled`,
 *   `live_unavailable`, `registry_enforcement_required`;
 * - the per-class counts, people and the PPE violation beside the image;
 * - never a device-local URL; stop when hidden.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { TelemetryApiError } from '../../services/api';
import LiveTile, { NOT_SENDING, NO_LIVE_SOURCE, PAUSED_NOTICE, safeFrameUrl, type FrameFetcher, type LeaseFetcher } from './LiveTile';
import type { LiveFrame, LiveFrameResponse, LiveLeaseResponse } from '../../pages/telemetry/types';
import { NOW, installResizeObserver } from '../../pages/telemetry/__fixtures__/telemetryFixtures';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

const INSTANCE_A = '0f1e2d3c-4b5a-4968-8776-65544332211a';
const INSTANCE_B = '1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';

function lease(overrides: Partial<LiveLeaseResponse> = {}): LiveLeaseResponse {
  return {
    device_id: 'thing-a',
    workflow_id: 'wf-1',
    slot: 0,
    startedAt: NOW,
    leaseExpiresAt: NOW + 60_000,
    expiresAt: NOW + 60_000,
    renewSeconds: 20,
    region: 'us-east-1',
    region_source: 'header',
    ...overrides,
  };
}

function frame(url: string, overrides: Partial<LiveFrame> = {}): LiveFrame {
  return {
    url,
    width: 1280,
    height: 720,
    ts: NOW - 2_000,
    age_s: 2,
    perClass: { helmet: 2, human: 3 },
    people: 3,
    violation: true,
    reasons: { 'no-helmet': 1 },
    expires_in: 120,
    ...overrides,
  };
}

const URL_1 = 'https://bucket.s3.example.com/live/0.jpg?sig=1';
const URL_2 = 'https://bucket.s3.example.com/live/0.jpg?sig=2';

function refusal(status: number, code: string, extra: Record<string, unknown> = {}) {
  return new TelemetryApiError(`refused with ${code}`, status, { error: `refused with ${code}`, error_code: code, ...extra });
}

/** A frame fetcher answering from a plan keyed by the poll index (the last entry repeats). */
function plannedFrames(plan: Array<LiveFrameResponse | Error>) {
  const haves: Array<string | null | undefined> = [];
  const fetchFrame = vi.fn<FrameFetcher>(async (_id, _device, have) => {
    haves.push(have);
    const index = Math.min(haves.length - 1, plan.length - 1);
    const planned = plan[index];
    if (planned instanceof Error) throw planned;
    return planned;
  });
  return { fetchFrame, haves };
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

function mount(fetchLease: LeaseFetcher, fetchFrame: FrameFetcher, deviceId: string | null = 'thing-a') {
  return render(<LiveTile dashboardId="dash-1" deviceId={deviceId} fetchLease={fetchLease} fetchFrame={fetchFrame} />);
}

const okLease = () => vi.fn<LeaseFetcher>(async () => lease());

beforeAll(() => {
  installResizeObserver();
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  setVisibility('visible');
});

afterEach(() => {
  vi.useRealTimers();
  setVisibility('visible');
});

describe('LiveTile — lease and frame cadence (design §9.3)', () => {
  it('takes the lease on show and renews it every 20 s', async () => {
    const fetchLease = okLease();
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null, state: 'no_live_source' }]);
    mount(fetchLease, fetchFrame);
    await advance(0);
    expect(fetchLease).toHaveBeenCalledTimes(1);
    expect(fetchLease).toHaveBeenCalledWith('dash-1', 'thing-a');
    await advance(19_000);
    expect(fetchLease).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(fetchLease).toHaveBeenCalledTimes(2);
    await advance(20_000);
    expect(fetchLease).toHaveBeenCalledTimes(3);
  });

  it('polls the frame every 2 s with have = the previous answer\'s frame_token', async () => {
    const { fetchFrame, haves } = plannedFrames([
      { frame: frame(URL_1), frame_token: `${INSTANCE_A}:41`, age_s: 2 },
      { frame: null, frame_token: `${INSTANCE_A}:41`, age_s: 3 },
    ]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(fetchFrame).toHaveBeenCalledTimes(1);
    expect(haves[0] ?? null).toBeNull();
    await advance(2_000);
    expect(fetchFrame).toHaveBeenCalledTimes(2);
    expect(haves[1]).toBe(`${INSTANCE_A}:41`);
    await advance(2_000);
    expect(haves[2]).toBe(`${INSTANCE_A}:41`);
    await advance(4_000);
    expect(fetchFrame).toHaveBeenCalledTimes(5);
    for (const have of haves.slice(1)) expect(have).toBe(`${INSTANCE_A}:41`);
  });

  it('keeps the image on frame: null and swaps it on a new token', async () => {
    const { fetchFrame } = plannedFrames([
      { frame: frame(URL_1), frame_token: `${INSTANCE_A}:41`, age_s: 1 },
      { frame: null, frame_token: `${INSTANCE_A}:41`, age_s: 3 },
      { frame: frame(URL_2, { perClass: { helmet: 1 }, people: 1, violation: false, reasons: {} }), frame_token: `${INSTANCE_A}:42`, age_s: 1 },
    ]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-img').getAttribute('src')).toBe(URL_1);
    await advance(2_000);
    expect(screen.getByTestId('telemetry-live-img').getAttribute('src')).toBe(URL_1);
    await advance(2_000);
    expect(screen.getByTestId('telemetry-live-img').getAttribute('src')).toBe(URL_2);
    expect(screen.getByTestId('telemetry-live-per-class').textContent).toBe('helmet 1');
    expect(screen.getByTestId('telemetry-live-violation').textContent).toBe('no violation');
  });

  it('a restart (new instance, seq 0) shows the new frame on the next poll and sends the new token', async () => {
    const { fetchFrame, haves } = plannedFrames([
      { frame: frame(URL_1), frame_token: `${INSTANCE_A}:4000`, age_s: 1 },
      { frame: null, frame_token: `${INSTANCE_A}:4000`, age_s: 2 },
      { frame: frame(URL_2), frame_token: `${INSTANCE_B}:0`, age_s: 1 },
      { frame: null, frame_token: `${INSTANCE_B}:0`, age_s: 2 },
    ]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-img').getAttribute('src')).toBe(URL_1);
    await advance(2_000);
    expect(haves[1]).toBe(`${INSTANCE_A}:4000`);
    await advance(2_000);
    expect(screen.getByTestId('telemetry-live-img').getAttribute('src')).toBe(URL_2);
    await advance(2_000);
    expect(haves[3]).toBe(`${INSTANCE_B}:0`);
    expect(screen.getByTestId('telemetry-live-state').textContent).toContain('live');
  });

  it('shows the per-class counts, the people and the PPE violation beside the image', async () => {
    const { fetchFrame } = plannedFrames([{ frame: frame(URL_1), frame_token: `${INSTANCE_A}:1`, age_s: 1 }]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-per-class').textContent).toBe('human 3 · helmet 2');
    expect(screen.getByTestId('telemetry-live-people').textContent).toBe('3');
    expect(screen.getByTestId('telemetry-live-violation').textContent).toBe('violation (no-helmet)');
    expect(screen.getByTestId('telemetry-live-img').getAttribute('alt')).toBe('Live frame of thing-a');
  });

  it('never shows a device-local URL', async () => {
    const { fetchFrame } = plannedFrames([{ frame: frame('http://device-local-host/frame.jpg'), frame_token: `${INSTANCE_A}:1`, age_s: 1 }]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.queryByTestId('telemetry-live-img')).toBeNull();
    expect(screen.getByTestId('telemetry-live-placeholder').textContent).toContain('not shown');
    expect(safeFrameUrl('http://device-local-host/frame.jpg')).toBeNull();
    expect(safeFrameUrl(URL_1)).toBe(URL_1);
    expect(safeFrameUrl(null)).toBeNull();
  });
});

describe('LiveTile — the states (design §9.3)', () => {
  it('no live source', async () => {
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null, state: 'no_live_source' }]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-state').textContent).toBe(NO_LIVE_SOURCE);
  });

  it('starting, then "the device is not sending" after 15 s without a frame', async () => {
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null, age_s: 0 }]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-state').textContent).toBe('starting');
    await advance(14_000);
    expect(screen.getByTestId('telemetry-live-state').textContent).toBe('starting');
    await advance(1_500);
    expect(screen.getByTestId('telemetry-live-state').textContent).toContain(NOT_SENDING);
  });

  it('live while the frame is at most 10 s old, stale after', async () => {
    const { fetchFrame } = plannedFrames([
      { frame: frame(URL_1, { age_s: 1 }), frame_token: `${INSTANCE_A}:1`, age_s: 1 },
      { frame: null, frame_token: `${INSTANCE_A}:1`, age_s: 3 },
    ]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-state').textContent).toMatch(/^live/);
    await advance(12_000);
    expect(screen.getByTestId('telemetry-live-state').textContent).toMatch(/^stale/);
    expect(screen.getByTestId('telemetry-live-img').getAttribute('src')).toBe(URL_1);
  });

  it('unavailable with the reason', async () => {
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: `${INSTANCE_A}:1`, age_s: 1, unavailable: 'frame_too_large' }]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-state').textContent).toBe('unavailable: frame_too_large');
  });

  it('live_viewing_limit pauses the tile with a resume action that leases again', async () => {
    let calls = 0;
    const fetchLease = vi.fn<LeaseFetcher>(async () => {
      calls += 1;
      if (calls === 2) throw refusal(409, 'live_viewing_limit', { max_minutes: 30 });
      return lease();
    });
    const { fetchFrame } = plannedFrames([{ frame: frame(URL_1), frame_token: `${INSTANCE_A}:1`, age_s: 1 }]);
    mount(fetchLease, fetchFrame);
    await advance(0);
    await advance(20_000);
    expect(screen.getByTestId('telemetry-live-state').textContent).toBe('paused');
    expect(screen.getByTestId('telemetry-live-notice-live_viewing_limit').textContent).toBe(PAUSED_NOTICE);
    const framesWhilePaused = fetchFrame.mock.calls.length;
    await advance(30_000);
    expect(fetchFrame).toHaveBeenCalledTimes(framesWhilePaused);
    expect(fetchLease).toHaveBeenCalledTimes(2);

    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-live-resume'));
    });
    await advance(0);
    expect(fetchLease).toHaveBeenCalledTimes(3);
    expect(screen.queryByTestId('telemetry-live-notice-live_viewing_limit')).toBeNull();
    expect(screen.getByTestId('telemetry-live-state').textContent).toMatch(/^live/);
  });

  it('renders the select-a-device state without a device and makes no request', async () => {
    const fetchLease = okLease();
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null }]);
    mount(fetchLease, fetchFrame, null);
    await advance(10_000);
    expect(fetchLease).not.toHaveBeenCalled();
    expect(fetchFrame).not.toHaveBeenCalled();
    expect(screen.getByTestId('telemetry-live-state').textContent).toBe('select a device');
  });
});

describe('LiveTile — the notices (design §11.3, §9.3)', () => {
  it('live_capacity_reached is a notice beside the tile; the lease is retried at the cadence', async () => {
    const fetchLease = vi.fn<LeaseFetcher>(async () => {
      throw refusal(429, 'live_capacity_reached', { reason: 'usecase_slots', max: 5, remedy: "Wait for another viewer's lease to lapse." });
    });
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null, state: 'no_live_source' }]);
    mount(fetchLease, fetchFrame);
    await advance(0);
    const notice = screen.getByTestId('telemetry-live-notice-live_capacity_reached');
    expect(notice.textContent).toContain('No live slot is available (usecase_slots)');
    expect(notice.textContent).toContain('lease to lapse');
    expect(screen.getByTestId('telemetry-live-image')).toBeInTheDocument();
    await advance(20_000);
    expect(fetchLease).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['telemetry_disabled', 'Telemetry is off for this use case.'],
    ['live_unavailable', 'Live frames are unavailable (media_retention_unset).'],
    ['registry_enforcement_required', 'Live frames need registry enforcement on.'],
  ] as const)('%s replaces the tile and stops the polls', async (code, text) => {
    const fetchLease = vi.fn<LeaseFetcher>(async () => {
      throw refusal(409, code, code === 'live_unavailable' ? { reason: 'media_retention_unset' } : {});
    });
    const { fetchFrame } = plannedFrames([{ frame: frame(URL_1), frame_token: `${INSTANCE_A}:1`, age_s: 1 }]);
    mount(fetchLease, fetchFrame);
    await advance(0);
    expect(screen.getByTestId(`telemetry-live-notice-${code}`).textContent).toContain(text);
    expect(screen.queryByTestId('telemetry-live-image')).toBeNull();
    const leases = fetchLease.mock.calls.length;
    const frames = fetchFrame.mock.calls.length;
    await advance(60_000);
    expect(fetchLease).toHaveBeenCalledTimes(leases);
    expect(fetchFrame).toHaveBeenCalledTimes(frames);
    expect(screen.getByTestId('telemetry-live-resume').textContent).toBe('Retry');
  });

  it('a registry_enforcement_required on the frame route also replaces the tile', async () => {
    const { fetchFrame } = plannedFrames([refusal(409, 'registry_enforcement_required')]);
    mount(okLease(), fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-notice-registry_enforcement_required')).toBeInTheDocument();
    expect(screen.queryByTestId('telemetry-live-image')).toBeNull();
  });

  it('a live_lease_failed shows the remedy and retries', async () => {
    const fetchLease = vi.fn<LeaseFetcher>(async () => {
      throw refusal(502, 'live_lease_failed', { reason: 'thing_not_found', remedy: "check that the device is online and the use case's role is in place" });
    });
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null, state: 'no_live_source' }]);
    mount(fetchLease, fetchFrame);
    await advance(0);
    expect(screen.getByTestId('telemetry-live-notice-live_lease_failed').textContent).toContain('device is online');
    await advance(20_000);
    expect(fetchLease).toHaveBeenCalledTimes(2);
  });
});

describe('LiveTile — visibility and device changes', () => {
  it('stops both loops when hidden and resumes them when visible', async () => {
    const fetchLease = okLease();
    const { fetchFrame } = plannedFrames([{ frame: frame(URL_1), frame_token: `${INSTANCE_A}:1`, age_s: 1 }]);
    mount(fetchLease, fetchFrame);
    await advance(0);
    expect(fetchLease).toHaveBeenCalledTimes(1);
    expect(fetchFrame).toHaveBeenCalledTimes(1);

    act(() => setVisibility('hidden'));
    await advance(60_000);
    expect(fetchLease).toHaveBeenCalledTimes(1);
    expect(fetchFrame).toHaveBeenCalledTimes(1);

    act(() => setVisibility('visible'));
    await advance(0);
    expect(fetchLease).toHaveBeenCalledTimes(2);
    expect(fetchFrame).toHaveBeenCalledTimes(2);
    await advance(2_000);
    expect(fetchFrame).toHaveBeenCalledTimes(3);
  });

  it('a device change leases the new device and forgets the old token', async () => {
    const fetchLease = okLease();
    const { fetchFrame, haves } = plannedFrames([{ frame: frame(URL_1), frame_token: `${INSTANCE_A}:1`, age_s: 1 }]);
    const view = render(<LiveTile dashboardId="dash-1" deviceId="thing-a" fetchLease={fetchLease} fetchFrame={fetchFrame} />);
    await advance(2_000);
    expect(haves[1]).toBe(`${INSTANCE_A}:1`);
    view.rerender(<LiveTile dashboardId="dash-1" deviceId="thing-b" fetchLease={fetchLease} fetchFrame={fetchFrame} />);
    await advance(0);
    expect(fetchLease).toHaveBeenLastCalledWith('dash-1', 'thing-b');
    expect(fetchFrame.mock.calls.at(-1)![1]).toBe('thing-b');
    expect(haves.at(-1) ?? null).toBeNull();
  });

  it('stops on unmount', async () => {
    const fetchLease = okLease();
    const { fetchFrame } = plannedFrames([{ frame: null, frame_token: null }]);
    const view = mount(fetchLease, fetchFrame);
    await advance(0);
    view.unmount();
    await advance(60_000);
    expect(fetchLease).toHaveBeenCalledTimes(1);
    expect(fetchFrame).toHaveBeenCalledTimes(1);
  });
});
