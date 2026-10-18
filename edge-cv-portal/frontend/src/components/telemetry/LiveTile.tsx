/**
 * The live tile (workflow-telemetry-dashboards design §9.3, §11.3; tasks.md 5.4).
 *
 * One selected Device: `acquireLiveLease` on show and every `renewSeconds`
 * (20) while visible and unpaused; `getLiveFrame` every 2 s passing the
 * previous answer's `frame_token` as `have`, keeping the image on
 * `frame: null` and swapping it on a new token (a LocalServer restart yields
 * a new `instance` and `seq` 0, hence a new token and the new frame on the
 * next poll). States: no live source, starting ("the device is not sending"
 * after 15 s), live (≤ 10 s), stale, paused, unavailable with the reason.
 * `live_viewing_limit` pauses the tile with a resume action;
 * `live_capacity_reached`, `telemetry_disabled`, `live_unavailable` and
 * `registry_enforcement_required` render as notices in place of the tile.
 * The per-class counts, the people count and the PPE violation state sit
 * beside the `<img>`, whose source is only ever the presigned URL the API
 * answered: never a device-local URL.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type { LiveFrame, LiveFrameResponse, LiveLeaseResponse, TelemetryCadence } from '../../pages/telemetry/types';
import { formatCount, formatDuration } from './format';

export const DEFAULT_FRAME_POLL_SECONDS = 2;
export const DEFAULT_LEASE_RENEW_SECONDS = 20;
/** A frame older than this is "stale" (design §9.3). */
export const LIVE_MAX_AGE_S = 10;
/** "the device is not sending" after this long without a frame under a lease. */
export const NOT_SENDING_AFTER_MS = 15_000;

export const NOT_SENDING = 'the device is not sending';
export const NO_LIVE_SOURCE = 'no live source';
export const PAUSED_NOTICE = 'The live tile paused after the continuous viewing limit; resume to start a new viewing period.';

export type LiveState = 'no_live_source' | 'starting' | 'live' | 'stale' | 'paused' | 'unavailable' | 'off';

export type LeaseFetcher = (dashboardId: string, deviceId: string) => Promise<LiveLeaseResponse>;
export type FrameFetcher = (dashboardId: string, deviceId: string, have?: string | null) => Promise<LiveFrameResponse>;

export interface LiveTileProps {
  dashboardId: string;
  /** The selected Device; null renders the "select a device" state. */
  deviceId: string | null;
  cadence?: Partial<TelemetryCadence> | null;
  fetchLease?: LeaseFetcher;
  fetchFrame?: FrameFetcher;
}

/** Refusals that stop the tile until the device changes or the viewer retries. */
const STOPPING_CODES = new Set(['telemetry_disabled', 'live_unavailable', 'registry_enforcement_required']);

interface Notice {
  type: 'info' | 'warning' | 'error';
  code: string;
  text: string;
  /** True when the notice replaces the tile (design §11.4). */
  replaces: boolean;
}

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

/** Only an https URL the API answered is ever shown; a device-local URL never is. */
export function safeFrameUrl(url: string | null | undefined): string | null {
  if (typeof url !== 'string') return null;
  return /^https:\/\//i.test(url) ? url : null;
}

function noticeFor(err: unknown): Notice {
  if (isTelemetryApiError(err)) {
    const code = err.errorCode ?? 'error';
    const reason = err.reason ? ` (${err.reason})` : '';
    const remedy = err.remedy ? ` ${err.remedy}` : '';
    switch (code) {
      case 'registry_enforcement_required':
        return { type: 'info', code, text: `Live frames need registry enforcement on.${remedy}`, replaces: true };
      case 'telemetry_disabled':
        return { type: 'info', code, text: `Telemetry is off for this use case.${remedy}`, replaces: true };
      case 'live_unavailable':
        return { type: 'info', code, text: `Live frames are unavailable${reason}.${remedy}`, replaces: true };
      case 'live_capacity_reached':
        return { type: 'warning', code, text: `No live slot is available${reason}.${remedy}`, replaces: false };
      case 'live_lease_failed':
        return { type: 'error', code, text: `${err.message}${reason}.${remedy}`, replaces: false };
      default:
        return { type: 'error', code, text: `${err.message}${reason}${remedy}`, replaces: false };
    }
  }
  return { type: 'error', code: 'error', text: getErrorMessage(err, 'The live lease could not be taken'), replaces: false };
}

export default function LiveTile({ dashboardId, deviceId, cadence, fetchLease, fetchFrame }: LiveTileProps) {
  const leaseFetcher: LeaseFetcher = useMemo(
    () => fetchLease ?? ((id, device) => apiService.acquireLiveLease(id, device)),
    [fetchLease]
  );
  const frameFetcher: FrameFetcher = useMemo(
    () => fetchFrame ?? ((id, device, have) => apiService.getLiveFrame(id, device, have)),
    [fetchFrame]
  );
  const framePeriodMs = Math.max(1, cadence?.liveFrame ?? DEFAULT_FRAME_POLL_SECONDS) * 1000;
  const defaultRenewMs = Math.max(1, cadence?.liveLease ?? DEFAULT_LEASE_RENEW_SECONDS) * 1000;

  const [lease, setLease] = useState<LiveLeaseResponse | null>(null);
  const [leaseGrantedAt, setLeaseGrantedAt] = useState<number | null>(null);
  const [frame, setFrame] = useState<LiveFrame | null>(null);
  const [frameAt, setFrameAt] = useState<number | null>(null);
  const [ageS, setAgeS] = useState<number | null>(null);
  const [noLiveSource, setNoLiveSource] = useState(false);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [stopped, setStopped] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [clock, setClock] = useState(() => Date.now());
  const tokenRef = useRef<string | null>(null);

  // A clock for the "starting → not sending" and the live/stale ages.
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);

  // Reset on a device change.
  useEffect(() => {
    tokenRef.current = null;
    setLease(null);
    setLeaseGrantedAt(null);
    setFrame(null);
    setFrameAt(null);
    setAgeS(null);
    setNoLiveSource(false);
    setUnavailable(null);
    setPaused(false);
    setNotice(null);
    setStopped(false);
  }, [dashboardId, deviceId]);

  useEffect(() => {
    if (!deviceId || paused || stopped) return undefined;
    let alive = true;
    let leaseTimer: ReturnType<typeof setTimeout> | null = null;
    let frameTimer: ReturnType<typeof setTimeout> | null = null;
    let leaseInFlight = false;
    let frameInFlight = false;
    let renewMs = defaultRenewMs;
    let halted = false;

    const clearTimers = () => {
      if (leaseTimer !== null) clearTimeout(leaseTimer);
      if (frameTimer !== null) clearTimeout(frameTimer);
      leaseTimer = null;
      frameTimer = null;
    };

    const halt = () => {
      halted = true;
      clearTimers();
    };

    const takeLease = async () => {
      if (!alive || leaseInFlight || halted) return;
      if (leaseTimer !== null) {
        clearTimeout(leaseTimer);
        leaseTimer = null;
      }
      leaseInFlight = true;
      try {
        const granted = await leaseFetcher(dashboardId, deviceId);
        if (!alive) return;
        renewMs = Math.max(1, granted.renewSeconds || DEFAULT_LEASE_RENEW_SECONDS) * 1000;
        setLease(granted);
        setLeaseGrantedAt((at) => at ?? Date.now());
        setNotice((n) => (n && !n.replaces ? null : n));
      } catch (err) {
        if (!alive) return;
        if (isTelemetryApiError(err) && err.errorCode === 'live_viewing_limit') {
          setPaused(true);
          setNotice({ type: 'info', code: 'live_viewing_limit', text: PAUSED_NOTICE, replaces: false });
          halt();
          return;
        }
        const next = noticeFor(err);
        setNotice(next);
        if (STOPPING_CODES.has(next.code)) {
          setStopped(true);
          halt();
          return;
        }
      } finally {
        leaseInFlight = false;
      }
      if (alive && !halted && !isHidden()) leaseTimer = setTimeout(() => void takeLease(), renewMs);
    };

    const pollFrame = async () => {
      if (!alive || frameInFlight || halted) return;
      if (frameTimer !== null) {
        clearTimeout(frameTimer);
        frameTimer = null;
      }
      frameInFlight = true;
      try {
        const answer = await frameFetcher(dashboardId, deviceId, tokenRef.current);
        if (!alive) return;
        if (answer.state === 'no_live_source') {
          setNoLiveSource(true);
        } else {
          setNoLiveSource(false);
          if (typeof answer.age_s === 'number') setAgeS(answer.age_s);
          if (answer.frame_token) tokenRef.current = answer.frame_token;
          if (answer.unavailable) {
            setUnavailable(answer.reason ? `${answer.unavailable} (${answer.reason})` : answer.unavailable);
          } else if (answer.frame) {
            setUnavailable(null);
            setFrame(answer.frame);
            setFrameAt(Date.now());
          }
          // `frame: null` with the echoed token: keep the image (design §9.3).
        }
      } catch (err) {
        if (!alive) return;
        if (isTelemetryApiError(err) && STOPPING_CODES.has(err.errorCode ?? '')) {
          setNotice(noticeFor(err));
          setStopped(true);
          halt();
          return;
        }
        if (isTelemetryApiError(err) && err.errorCode === 'invalid_token') {
          tokenRef.current = null;
        }
      } finally {
        frameInFlight = false;
      }
      if (alive && !halted && !isHidden()) frameTimer = setTimeout(() => void pollFrame(), framePeriodMs);
    };

    const onVisibility = () => {
      if (isHidden()) {
        clearTimers();
      } else if (!halted) {
        if (!leaseInFlight && leaseTimer === null) void takeLease();
        if (!frameInFlight && frameTimer === null) void pollFrame();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    void takeLease();
    void pollFrame();

    return () => {
      alive = false;
      clearTimers();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // The fetchers and cadence are stable per page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboardId, deviceId, paused, stopped, generation]);

  const resume = useCallback(() => {
    setPaused(false);
    setStopped(false);
    setNotice(null);
    setLeaseGrantedAt(null);
    setGeneration((g) => g + 1);
  }, []);

  const frameAgeS = useMemo(() => {
    if (!frame) return null;
    const base = ageS ?? frame.age_s ?? 0;
    const elapsed = frameAt ? Math.max(0, (clock - frameAt) / 1000) : 0;
    return Math.round(base + elapsed);
  }, [frame, ageS, frameAt, clock]);

  const state: LiveState = !deviceId
    ? 'off'
    : paused
      ? 'paused'
      : unavailable
        ? 'unavailable'
        : frame
          ? frameAgeS !== null && frameAgeS <= LIVE_MAX_AGE_S
            ? 'live'
            : 'stale'
          : noLiveSource
            ? 'no_live_source'
            : 'starting';

  const notSending = state === 'starting' && leaseGrantedAt !== null && clock - leaseGrantedAt >= NOT_SENDING_AFTER_MS;

  const indicator: { type: 'success' | 'warning' | 'error' | 'pending' | 'stopped' | 'info'; text: string } = (() => {
    switch (state) {
      case 'live':
        return { type: 'success', text: `live · ${formatDuration(frameAgeS ?? 0)} old` };
      case 'stale':
        return { type: 'warning', text: `stale · ${formatDuration(frameAgeS ?? 0)} old` };
      case 'paused':
        return { type: 'stopped', text: 'paused' };
      case 'unavailable':
        return { type: 'error', text: `unavailable: ${unavailable}` };
      case 'no_live_source':
        return { type: 'stopped', text: NO_LIVE_SOURCE };
      case 'starting':
        return notSending ? { type: 'warning', text: `starting · ${NOT_SENDING}` } : { type: 'pending', text: 'starting' };
      default:
        return { type: 'stopped', text: 'select a device' };
    }
  })();

  const violationText = !frame
    ? '—'
    : frame.violation === true
      ? `violation${Object.keys(frame.reasons ?? {}).length ? ` (${Object.keys(frame.reasons).join(', ')})` : ''}`
      : frame.violation === false
        ? 'no violation'
        : 'not reported';
  const perClass = frame ? Object.entries(frame.perClass ?? {}).sort((a, b) => b[1] - a[1]) : [];
  const imageUrl = safeFrameUrl(frame?.url);

  return (
    <Container
      data-testid="telemetry-live-tile"
      header={
        <Header
          variant="h2"
          description={deviceId ? `${deviceId}${lease ? ` · lease slot ${lease.slot}, renewed every ${Math.round((lease.renewSeconds || DEFAULT_LEASE_RENEW_SECONDS))} s` : ''}` : 'Select a device to view its live frames'}
          actions={
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              <StatusIndicator type={indicator.type}>
                <span data-testid="telemetry-live-state">{indicator.text}</span>
              </StatusIndicator>
              {(paused || stopped) && deviceId && (
                <Button onClick={resume} data-testid="telemetry-live-resume">
                  {paused ? 'Resume' : 'Retry'}
                </Button>
              )}
            </SpaceBetween>
          }
        >
          Live
        </Header>
      }
    >
      <SpaceBetween size="s">
        {notice && (
          <Alert type={notice.type} data-testid={`telemetry-live-notice-${notice.code}`}>
            {notice.text}
          </Alert>
        )}
        {deviceId && !(notice && notice.replaces) && (
          <SpaceBetween direction="horizontal" size="l">
            <div data-testid="telemetry-live-image">
              {imageUrl ? (
                <img
                  src={imageUrl}
                  alt={`Live frame of ${deviceId}`}
                  width={frame?.width ?? undefined}
                  height={frame?.height ?? undefined}
                  style={{ maxWidth: '640px', width: '100%', height: 'auto' }}
                  data-testid="telemetry-live-img"
                />
              ) : (
                <Box color="text-body-secondary" padding="l" data-testid="telemetry-live-placeholder">
                  {frame ? 'The frame URL is not a Portal-signed address and is not shown' : indicator.text}
                </Box>
              )}
            </div>
            <KeyValuePairs
              columns={1}
              items={[
                {
                  label: 'Per class',
                  value: (
                    <span data-testid="telemetry-live-per-class">
                      {perClass.length ? perClass.map(([label, count]) => `${label} ${count}`).join(' · ') : '—'}
                    </span>
                  ),
                },
                { label: 'People', value: <span data-testid="telemetry-live-people">{frame ? formatCount(frame.people) : '—'}</span> },
                { label: 'PPE', value: <span data-testid="telemetry-live-violation">{violationText}</span> },
                { label: 'Frame', value: <span>{frame ? `${frame.width ?? '?'} × ${frame.height ?? '?'}` : '—'}</span> },
              ]}
            />
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}
