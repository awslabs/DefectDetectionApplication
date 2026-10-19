/**
 * Unit tests for the clip gallery (workflow-telemetry-dashboards task 5.4,
 * Requirements 12.1-12.4, 17.5; design §9.2, §11.3).
 *
 * - each entry state from fixtures: playable, "upload failed on the device"
 *   with the plain-text `s3Error`, "expired or missing", "not allowed" with
 *   its reason, "unavailable";
 * - the media request only on Play (never on listing), the `<video controls
 *   preload="metadata">` player and the download link, one fresh-URL retry on
 *   a playback error before expiry and none after;
 * - a 409 `registry_enforcement_required` as a notice in place of playback;
 * - the 15 s refresh while visible, stopped when hidden; the device filter and
 *   the cursor paging.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { TelemetryApiError } from '../../services/api';
import ClipGallery, {
  ENFORCEMENT_NOTICE,
  EXPIRED_OR_MISSING,
  NOT_ALLOWED,
  UNAVAILABLE,
  UPLOAD_FAILED,
  type ClipMediaFetcher,
  type ClipsFetcher,
} from './ClipGallery';
import type { ClipMediaResponse, DashboardClipsResponse, TelemetryClip } from '../../pages/telemetry/types';
import { NOW, installResizeObserver } from '../../pages/telemetry/__fixtures__/telemetryFixtures';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

function clip(overrides: Partial<TelemetryClip> = {}): TelemetryClip {
  return {
    clip_ref: 'cmVmLTE',
    clipId: 'clip-0001',
    deviceId: 'thing-a',
    workflowVersion: '3',
    startTs: NOW - 120_000,
    endTs: NOW - 108_000,
    durationS: 12,
    reasons: { 'no-helmet': 3 },
    totals: {},
    file: {},
    revision: 1,
    state: 'playable',
    receivedMs: NOW - 100_000,
    hasSidecar: true,
    ...overrides,
  };
}

const FIVE_STATES: TelemetryClip[] = [
  clip(),
  clip({ clip_ref: 'cmVmLTI', clipId: 'clip-0002', state: 'upload_failed', s3Error: 'AccessDenied: the device role lacks PutObject' }),
  clip({ clip_ref: 'cmVmLTM', clipId: 'clip-0003', state: 'expired_or_missing' }),
  clip({ clip_ref: 'cmVmLTQ', clipId: 'clip-0004', state: 'not_allowed', reason: 'key_outside_prefix' }),
  clip({ clip_ref: 'cmVmLTU', clipId: 'clip-0005', state: 'unavailable', reason: 'AccessDenied', note: 'An override bucket is readable only when it is tagged.' }),
];

function listing(clips: TelemetryClip[] = FIVE_STATES, cursor: string | null = null): DashboardClipsResponse {
  return { clips, cursor, from: NOW - 90 * 86_400_000, to: NOW };
}

function media(overrides: Partial<ClipMediaResponse> = {}): ClipMediaResponse {
  return {
    video_url: 'https://bucket.s3.example.com/clips/clip-0001.mp4?X-Amz-Signature=one',
    download_url: 'https://bucket.s3.example.com/clips/clip-0001.mp4?response-content-disposition=attachment',
    sidecar_url: 'https://bucket.s3.example.com/clips/clip-0001.json',
    expires_in: 900,
    clipId: 'clip-0001',
    ...overrides,
  };
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

function mount(fetchClips: ClipsFetcher, fetchMedia: ClipMediaFetcher = vi.fn(async () => media())) {
  return render(<ClipGallery dashboardId="dash-1" deviceIds={['thing-a', 'thing-b']} fetchClips={fetchClips} fetchMedia={fetchMedia} />);
}

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

describe('ClipGallery — the entry states (design §9.2)', () => {
  it('renders each of the five states with its detail', async () => {
    const fetchClips = vi.fn<ClipsFetcher>(async () => listing());
    const fetchMedia = vi.fn<ClipMediaFetcher>(async () => media());
    mount(fetchClips, fetchMedia);
    await advance(0);

    expect(screen.getByTestId('telemetry-clip-clip-0001-state').textContent).toBe('playable');
    expect(screen.getByTestId('telemetry-clip-clip-0001-play')).toBeInTheDocument();
    expect(screen.getByTestId('telemetry-clip-clip-0002-state').textContent).toBe(UPLOAD_FAILED);
    expect(screen.getByTestId('telemetry-clip-clip-0002-detail').textContent).toBe('AccessDenied: the device role lacks PutObject');
    expect(screen.getByTestId('telemetry-clip-clip-0003-state').textContent).toBe(EXPIRED_OR_MISSING);
    expect(screen.getByTestId('telemetry-clip-clip-0004-state').textContent).toBe(NOT_ALLOWED);
    expect(screen.getByTestId('telemetry-clip-clip-0004-detail').textContent).toBe('key_outside_prefix');
    expect(screen.getByTestId('telemetry-clip-clip-0005-state').textContent).toBe(UNAVAILABLE);
    expect(screen.getByTestId('telemetry-clip-clip-0005-detail').textContent).toContain('AccessDenied');
    // No Play for the four other states and no media request on listing.
    for (const id of ['clip-0002', 'clip-0003', 'clip-0004', 'clip-0005']) {
      expect(screen.queryByTestId(`telemetry-clip-${id}-play`)).toBeNull();
    }
    expect(fetchMedia).not.toHaveBeenCalled();
  });

  it('renders the s3Error as plain text, not markup', async () => {
    const fetchClips = vi.fn<ClipsFetcher>(async () =>
      listing([clip({ clipId: 'clip-x', clip_ref: 'eA', state: 'upload_failed', s3Error: '<b>bold</b> & <img src=x>' })])
    );
    mount(fetchClips);
    await advance(0);
    const detail = screen.getByTestId('telemetry-clip-clip-x-detail');
    expect(detail.textContent).toBe('<b>bold</b> & <img src=x>');
    expect(detail.querySelector('b')).toBeNull();
    expect(detail.querySelector('img')).toBeNull();
  });

  it('shows the empty state without clips', async () => {
    mount(vi.fn<ClipsFetcher>(async () => listing([])));
    await advance(0);
    expect(screen.getByTestId('telemetry-clips-empty')).toBeInTheDocument();
  });
});

describe('ClipGallery — playback (design §9.2)', () => {
  it('requests the media only on Play, renders the player and the download link', async () => {
    const fetchMedia = vi.fn<ClipMediaFetcher>(async () => media());
    mount(vi.fn<ClipsFetcher>(async () => listing()), fetchMedia);
    await advance(0);
    expect(fetchMedia).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clip-clip-0001-play'));
    });
    await advance(0);

    expect(fetchMedia).toHaveBeenCalledTimes(1);
    expect(fetchMedia).toHaveBeenCalledWith('dash-1', 'cmVmLTE');
    const video = screen.getByTestId('telemetry-clip-clip-0001-video') as HTMLVideoElement;
    expect(video.getAttribute('src')).toBe(media().video_url);
    expect(video.hasAttribute('controls')).toBe(true);
    expect(video.getAttribute('preload')).toBe('metadata');
    const download = screen.getByTestId('telemetry-clip-clip-0001-download') as HTMLAnchorElement;
    expect(download.getAttribute('href')).toBe(media().download_url);
    expect(download.getAttribute('download')).toBe('clip-0001.mp4');
  });

  it('asks once for a fresh URL on a playback error before expiry, and not a second time', async () => {
    let calls = 0;
    const fetchMedia = vi.fn<ClipMediaFetcher>(async () => {
      calls += 1;
      return media({ video_url: `https://bucket.s3.example.com/clips/clip-0001.mp4?sig=${calls}` });
    });
    mount(vi.fn<ClipsFetcher>(async () => listing()), fetchMedia);
    await advance(0);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clip-clip-0001-play'));
    });
    await advance(0);
    expect(screen.getByTestId('telemetry-clip-clip-0001-video').getAttribute('src')).toContain('sig=1');

    await act(async () => {
      fireEvent.error(screen.getByTestId('telemetry-clip-clip-0001-video'));
    });
    await advance(0);
    expect(fetchMedia).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('telemetry-clip-clip-0001-video').getAttribute('src')).toContain('sig=2');

    await act(async () => {
      fireEvent.error(screen.getByTestId('telemetry-clip-clip-0001-video'));
    });
    await advance(0);
    expect(fetchMedia).toHaveBeenCalledTimes(2);
  });

  it('does not refresh the URL on an error after it expired', async () => {
    const fetchMedia = vi.fn<ClipMediaFetcher>(async () => media({ expires_in: 60 }));
    mount(vi.fn<ClipsFetcher>(async () => listing()), fetchMedia);
    await advance(0);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clip-clip-0001-play'));
    });
    await advance(61_000);
    await act(async () => {
      fireEvent.error(screen.getByTestId('telemetry-clip-clip-0001-video'));
    });
    await advance(0);
    expect(fetchMedia).toHaveBeenCalledTimes(1);
  });

  it('renders a 409 registry_enforcement_required as a notice in place of playback', async () => {
    const fetchMedia = vi.fn<ClipMediaFetcher>(async () => {
      throw new TelemetryApiError('Registry enforcement is off', 409, {
        error: 'Registry enforcement is off',
        error_code: 'registry_enforcement_required',
        remedy: 'Turn on registry enforcement (PORTAL_REGISTRY_ENFORCED) once the registry backfill is complete',
      });
    });
    mount(vi.fn<ClipsFetcher>(async () => listing()), fetchMedia);
    await advance(0);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clip-clip-0001-play'));
    });
    await advance(0);
    expect(screen.getByTestId('telemetry-clip-clip-0001-notice').textContent).toBe(ENFORCEMENT_NOTICE);
    expect(screen.queryByTestId('telemetry-clip-clip-0001-video')).toBeNull();
    expect(screen.queryByTestId('telemetry-clip-clip-0001-download')).toBeNull();
  });

  it('renders another refusal with its reason and remedy', async () => {
    const fetchMedia = vi.fn<ClipMediaFetcher>(async () => {
      throw new TelemetryApiError('The clip has no readable media', 409, {
        error: 'The clip has no readable media',
        error_code: 'clip_media_unavailable',
        reason: 'bucket_not_allowed',
      });
    });
    mount(vi.fn<ClipsFetcher>(async () => listing()), fetchMedia);
    await advance(0);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clip-clip-0001-play'));
    });
    await advance(0);
    expect(screen.getByTestId('telemetry-clip-clip-0001-notice').textContent).toContain('no readable media (bucket_not_allowed)');
  });
});

describe('ClipGallery — refresh, filter and paging', () => {
  it('refreshes every 15 s while visible and stops when hidden', async () => {
    const fetchClips = vi.fn<ClipsFetcher>(async () => listing());
    mount(fetchClips);
    await advance(0);
    expect(fetchClips).toHaveBeenCalledTimes(1);
    expect(fetchClips.mock.calls[0]).toEqual(['dash-1', {}]);
    await advance(14_000);
    expect(fetchClips).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(fetchClips).toHaveBeenCalledTimes(2);

    act(() => setVisibility('hidden'));
    await advance(60_000);
    expect(fetchClips).toHaveBeenCalledTimes(2);
    act(() => setVisibility('visible'));
    await advance(0);
    expect(fetchClips).toHaveBeenCalledTimes(3);
  });

  it('passes the device filter and pages with the cursor', async () => {
    const fetchClips = vi.fn<ClipsFetcher>(async (_id, query) =>
      query.cursor ? listing([clip({ clipId: 'clip-page2', clip_ref: 'cDI' })], null) : listing(FIVE_STATES, 'cursor-1')
    );
    const { container } = mount(fetchClips);
    await advance(0);
    expect(screen.getByTestId('telemetry-clips-page').textContent).toBe('page 1');

    const select = createWrapper(container).findSelect('[data-testid="telemetry-clips-device"]')!;
    select.openDropdown();
    select.selectOptionByValue('thing-b');
    await advance(0);
    expect(fetchClips).toHaveBeenLastCalledWith('dash-1', { device_id: 'thing-b' });

    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clips-next'));
    });
    await advance(0);
    expect(fetchClips).toHaveBeenLastCalledWith('dash-1', { device_id: 'thing-b', cursor: 'cursor-1' });
    expect(screen.getByTestId('telemetry-clips-page').textContent).toBe('page 2');
    expect(screen.getByTestId('telemetry-clip-clip-page2-state')).toBeInTheDocument();
    expect((screen.getByTestId('telemetry-clips-next') as HTMLButtonElement).disabled).toBe(true);

    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-clips-previous'));
    });
    await advance(0);
    expect(screen.getByTestId('telemetry-clips-page').textContent).toBe('page 1');
    expect(fetchClips).toHaveBeenLastCalledWith('dash-1', { device_id: 'thing-b' });
  });

  it('shows a listing failure', async () => {
    mount(vi.fn<ClipsFetcher>(async () => { throw new Error('boom'); }));
    await advance(0);
    expect(screen.getByTestId('telemetry-clips-error').textContent).toContain('boom');
  });
});
