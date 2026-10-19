/**
 * The clip gallery (workflow-telemetry-dashboards design §9.2, §11.3;
 * tasks.md 5.4).
 *
 * 25 clips per page, newest first, with a device filter; each entry's state
 * (`playable`, "upload failed on the device" with the plain-text `s3Error`,
 * "expired or missing", "not allowed" with its reason, "unavailable"); the
 * `<video controls preload="metadata">` player fed by `getClipMedia` on play
 * (never before), asking once for a fresh URL on an error before the URL
 * expires; the download link; a 409 `registry_enforcement_required` rendered
 * as a notice in place of playback; refresh every 15 s while visible.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  Header,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type { ClipMediaResponse, ClipsQuery, DashboardClipsResponse, TelemetryClip } from '../../pages/telemetry/types';
import { formatAge, formatDuration, formatTime } from './format';

export const CLIPS_PER_PAGE = 25;
export const DEFAULT_CLIPS_REFRESH_SECONDS = 15;
export const UPLOAD_FAILED = 'upload failed on the device';
export const EXPIRED_OR_MISSING = 'expired or missing';
export const NOT_ALLOWED = 'not allowed';
export const UNAVAILABLE = 'unavailable';
export const ENFORCEMENT_NOTICE =
  'Clip playback needs registry enforcement on (PORTAL_REGISTRY_ENFORCED); the listing works, playback and download do not.';

export type ClipsFetcher = (dashboardId: string, query: ClipsQuery) => Promise<DashboardClipsResponse>;
export type ClipMediaFetcher = (dashboardId: string, clipRef: string) => Promise<ClipMediaResponse>;

export interface ClipGalleryProps {
  dashboardId: string;
  /** The Dashboard's devices, for the filter. */
  deviceIds: readonly string[];
  /** The `clips` cadence of `/telemetry/schemas` (15 s by default). */
  refreshSeconds?: number;
  fetchClips?: ClipsFetcher;
  fetchMedia?: ClipMediaFetcher;
}

/** The label and indicator of an entry's state (design §9.2). */
export function clipStateView(clip: TelemetryClip): { type: 'success' | 'error' | 'warning' | 'stopped' | 'info'; label: string; detail?: string } {
  switch (clip.state) {
    case 'playable':
      return { type: 'success', label: 'playable' };
    case 'upload_failed':
      return { type: 'error', label: UPLOAD_FAILED, detail: clip.s3Error ?? undefined };
    case 'expired_or_missing':
      return { type: 'stopped', label: EXPIRED_OR_MISSING };
    case 'not_allowed':
      return { type: 'warning', label: NOT_ALLOWED, detail: clip.reason };
    default:
      return { type: 'warning', label: UNAVAILABLE, detail: [clip.reason, clip.note].filter(Boolean).join(' · ') || undefined };
  }
}

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

const ALL_DEVICES = '__all__';

interface PlayerState {
  clipRef: string;
  media: ClipMediaResponse | null;
  mintedAt: number;
  retried: boolean;
  loading: boolean;
  notice: string | null;
  enforcement: boolean;
}

export default function ClipGallery({ dashboardId, deviceIds, refreshSeconds, fetchClips, fetchMedia }: ClipGalleryProps) {
  const clipsFetcher: ClipsFetcher = useMemo(
    () => fetchClips ?? ((id, query) => apiService.getDashboardClips(id, query)),
    [fetchClips]
  );
  const mediaFetcher: ClipMediaFetcher = useMemo(
    () => fetchMedia ?? ((id, ref) => apiService.getClipMedia(id, ref)),
    [fetchMedia]
  );
  const periodMs = Math.max(1, refreshSeconds ?? DEFAULT_CLIPS_REFRESH_SECONDS) * 1000;

  const [deviceFilter, setDeviceFilter] = useState<string>(ALL_DEVICES);
  // The page stack: the cursor each page started from (null for the first).
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [answer, setAnswer] = useState<DashboardClipsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const [player, setPlayer] = useState<PlayerState | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const playerRef = useRef<PlayerState | null>(null);
  playerRef.current = player;

  const currentCursor = cursors[pageIndex] ?? null;

  useEffect(() => {
    let alive = true;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };
    const load = async () => {
      if (!alive || inFlight) return;
      clearTimer();
      inFlight = true;
      setLoading(true);
      try {
        const query: ClipsQuery = {};
        if (deviceFilter !== ALL_DEVICES) query.device_id = deviceFilter;
        if (currentCursor) query.cursor = currentCursor;
        const result = await clipsFetcher(dashboardId, query);
        if (!alive) return;
        setAnswer(result);
        setError(null);
        setClock(Date.now());
      } catch (err) {
        if (!alive) return;
        setError(getErrorMessage(err, 'The clips could not be listed'));
      } finally {
        inFlight = false;
        if (alive) setLoading(false);
      }
      if (alive && !isHidden()) timer = setTimeout(() => void load(), periodMs);
    };
    const onVisibility = () => {
      if (isHidden()) clearTimer();
      else if (!inFlight && timer === null) void load();
    };
    document.addEventListener('visibilitychange', onVisibility);
    void load();
    return () => {
      alive = false;
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [dashboardId, deviceFilter, currentCursor, periodMs, clipsFetcher, generation]);

  const changeDevice = (value: string) => {
    setDeviceFilter(value);
    setCursors([null]);
    setPageIndex(0);
    setPlayer(null);
  };

  const nextPage = () => {
    if (!answer?.cursor) return;
    setCursors((stack) => [...stack.slice(0, pageIndex + 1), answer.cursor]);
    setPageIndex((i) => i + 1);
    setPlayer(null);
  };

  const previousPage = () => {
    if (pageIndex === 0) return;
    setPageIndex((i) => i - 1);
    setPlayer(null);
  };

  /** Mint the URLs for a clip: on Play, and once more on a playback error before expiry. */
  const mint = useCallback(
    async (clipRef: string, retry: boolean) => {
      setPlayer((p) => ({
        clipRef,
        media: retry ? p?.media ?? null : null,
        mintedAt: p?.clipRef === clipRef ? p.mintedAt : 0,
        retried: retry ? true : false,
        loading: true,
        notice: null,
        enforcement: false,
      }));
      try {
        const media = await mediaFetcher(dashboardId, clipRef);
        setPlayer((p) => (p && p.clipRef === clipRef ? { ...p, media, mintedAt: Date.now(), loading: false, notice: null } : p));
      } catch (err) {
        const enforcement = isTelemetryApiError(err) && err.isEnforcementRequired;
        const text = isTelemetryApiError(err)
          ? `${err.message}${err.reason ? ` (${err.reason})` : ''}${err.remedy ? `. ${err.remedy}` : ''}`
          : getErrorMessage(err, 'The clip could not be opened');
        setPlayer((p) =>
          p && p.clipRef === clipRef ? { ...p, media: null, loading: false, notice: enforcement ? ENFORCEMENT_NOTICE : text, enforcement } : p
        );
      }
    },
    [dashboardId, mediaFetcher]
  );

  const onVideoError = () => {
    const current = playerRef.current;
    if (!current || !current.media || current.retried) return;
    const expiresAt = current.mintedAt + current.media.expires_in * 1000;
    if (Date.now() >= expiresAt) return;
    void mint(current.clipRef, true);
  };

  const deviceOptions: SelectProps.Option[] = useMemo(
    () => [{ label: 'All devices', value: ALL_DEVICES }, ...deviceIds.map((id) => ({ label: id, value: id }))],
    [deviceIds]
  );

  const clips = answer?.clips ?? [];

  return (
    <Container
      data-testid="telemetry-clip-gallery"
      header={
        <Header
          variant="h2"
          description={answer ? `${clips.length} clip(s) on this page · updated ${formatAge(clock, Date.now())} · refresh every ${periodMs / 1000} s while visible` : 'Reading the clips…'}
          actions={
            <SpaceBetween direction="horizontal" size="xs" alignItems="end">
              <Select
                selectedOption={deviceOptions.find((o) => o.value === deviceFilter) ?? deviceOptions[0]}
                onChange={({ detail }) => changeDevice(detail.selectedOption.value ?? ALL_DEVICES)}
                options={deviceOptions}
                selectedAriaLabel="Selected"
                data-testid="telemetry-clips-device"
              />
              <Button iconName="refresh" onClick={() => setGeneration((g) => g + 1)} disabled={loading} ariaLabel="Refresh the clips">
                Refresh
              </Button>
            </SpaceBetween>
          }
        >
          Clips
        </Header>
      }
      footer={
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={previousPage} disabled={pageIndex === 0} data-testid="telemetry-clips-previous">
            Previous
          </Button>
          <Box padding={{ top: 'xs' }} data-testid="telemetry-clips-page">
            page {pageIndex + 1}
          </Box>
          <Button onClick={nextPage} disabled={!answer?.cursor} data-testid="telemetry-clips-next">
            Next
          </Button>
        </SpaceBetween>
      }
    >
      <SpaceBetween size="s">
        {error && (
          <Alert type="error" data-testid="telemetry-clips-error">
            {error}
          </Alert>
        )}
        {!answer && loading && <StatusIndicator type="loading">Reading…</StatusIndicator>}
        {answer && clips.length === 0 && (
          <Box color="text-body-secondary" data-testid="telemetry-clips-empty">
            No clips yet
          </Box>
        )}
        {clips.map((clip) => {
          const view = clipStateView(clip);
          const open = player?.clipRef === clip.clip_ref;
          return (
            <Container key={clip.clip_ref} data-testid={`telemetry-clip-${clip.clipId}`} variant="stacked">
              <SpaceBetween size="xs">
                <SpaceBetween direction="horizontal" size="s" alignItems="center">
                  <Box fontWeight="bold">{clip.clipId}</Box>
                  <Badge color="grey">{clip.deviceId}</Badge>
                  <span>{formatTime(clip.startTs)}</span>
                  <span>{clip.durationS !== null ? formatDuration(clip.durationS) : '—'}</span>
                  <span>{Object.keys(clip.reasons ?? {}).join(', ') || 'no reason recorded'}</span>
                  <StatusIndicator type={view.type}>
                    <span data-testid={`telemetry-clip-${clip.clipId}-state`}>{view.label}</span>
                  </StatusIndicator>
                  {clip.state === 'playable' && !open && (
                    <Button onClick={() => void mint(clip.clip_ref, false)} data-testid={`telemetry-clip-${clip.clipId}-play`}>
                      Play
                    </Button>
                  )}
                  {open && (
                    <Button onClick={() => setPlayer(null)} data-testid={`telemetry-clip-${clip.clipId}-close`}>
                      Close
                    </Button>
                  )}
                </SpaceBetween>
                {view.detail && (
                  <Box variant="small" color="text-body-secondary" data-testid={`telemetry-clip-${clip.clipId}-detail`}>
                    {view.detail}
                  </Box>
                )}
                {open && player && (
                  <div data-testid={`telemetry-clip-${clip.clipId}-player`}>
                    {player.loading && <StatusIndicator type="loading">Opening…</StatusIndicator>}
                    {player.notice && (
                      <Alert type={player.enforcement ? 'info' : 'warning'} data-testid={`telemetry-clip-${clip.clipId}-notice`}>
                        {player.notice}
                      </Alert>
                    )}
                    {player.media && (
                      <SpaceBetween size="xs">
                        <video
                          controls
                          preload="metadata"
                          src={player.media.video_url}
                          onError={onVideoError}
                          style={{ maxWidth: '100%' }}
                          data-testid={`telemetry-clip-${clip.clipId}-video`}
                          aria-label={`Clip ${clip.clipId}`}
                        />
                        <SpaceBetween direction="horizontal" size="s">
                          <a href={player.media.download_url} download={`${clip.clipId}.mp4`} data-testid={`telemetry-clip-${clip.clipId}-download`}>
                            Download
                          </a>
                          {player.media.sidecar_url && (
                            <a href={player.media.sidecar_url} target="_blank" rel="noopener noreferrer">
                              Sidecar
                            </a>
                          )}
                          <Box variant="small" color="text-body-secondary">
                            URLs valid {formatDuration(player.media.expires_in)}
                          </Box>
                        </SpaceBetween>
                      </SpaceBetween>
                    )}
                  </div>
                )}
              </SpaceBetween>
            </Container>
          );
        })}
      </SpaceBetween>
    </Container>
  );
}
