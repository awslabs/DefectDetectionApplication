/**
 * Polls `GET /telemetry/dashboards/{id}/now` every `pollSeconds`
 * (`telemetryNowPollSeconds` from `/telemetry/schemas`, 5 by default) while
 * the document is visible, stopping when it is hidden and resuming at once
 * when it is shown again (workflow-telemetry-dashboards design §9.1).
 *
 * The Now and Health panels both render from this one answer: the dashboard
 * page polls once and hands the answer to both, so a page costs one `now`
 * request per poll (the figure of design §15.1), while a panel mounted on its
 * own polls for itself.
 */
import { useEffect, useMemo, useState } from 'react';
import { apiService } from '../services/api';
import { getErrorMessage } from '../utils/errorHandling';
import type { DashboardNowResponse } from '../pages/telemetry/types';

export const DEFAULT_NOW_POLL_SECONDS = 5;

export type NowFetcher = (dashboardId: string) => Promise<DashboardNowResponse>;

export interface UseDashboardNowOptions {
  /** The dashboard to poll; null disables the poller (a controlled panel). */
  dashboardId: string | null;
  pollSeconds?: number;
  /** Test seam; defaults to `apiService.getDashboardNow`. */
  fetchNow?: NowFetcher;
}

export interface DashboardNowState {
  now: DashboardNowResponse | null;
  loading: boolean;
  error: string | null;
  /** Polls completed so far (for tests and the first-load state). */
  polls: number;
  refresh: () => void;
}

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

export function useDashboardNow(options: UseDashboardNowOptions): DashboardNowState {
  const { dashboardId, pollSeconds = DEFAULT_NOW_POLL_SECONDS } = options;
  const fetchNow: NowFetcher = useMemo(
    () => options.fetchNow ?? ((id) => apiService.getDashboardNow(id)),
    [options.fetchNow]
  );
  const [state, setState] = useState<Omit<DashboardNowState, 'refresh'>>({
    now: null,
    loading: false,
    error: null,
    polls: 0,
  });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (!dashboardId) return undefined;
    let alive = true;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const periodMs = Math.max(1, pollSeconds) * 1000;

    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const poll = async () => {
      if (!alive || inFlight) return;
      clearTimer();
      inFlight = true;
      setState((s) => ({ ...s, loading: true }));
      try {
        const answer = await fetchNow(dashboardId);
        if (!alive) return;
        setState((s) => ({ now: answer, loading: false, error: null, polls: s.polls + 1 }));
      } catch (err) {
        if (!alive) return;
        setState((s) => ({ ...s, loading: false, error: getErrorMessage(err, 'The Now panel could not be read'), polls: s.polls + 1 }));
      } finally {
        inFlight = false;
      }
      if (alive && !isHidden()) timer = setTimeout(() => void poll(), periodMs);
    };

    const onVisibility = () => {
      if (isHidden()) {
        clearTimer();
      } else if (!inFlight && timer === null) {
        void poll();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    void poll();

    return () => {
      alive = false;
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [dashboardId, pollSeconds, fetchNow, generation]);

  const refresh = useMemo(() => () => setGeneration((g) => g + 1), []);

  return { ...state, refresh };
}
