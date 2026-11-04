/**
 * Assistant state shared by the Layout and the drawer (spec
 * portal-ai-assistant, design §12; Requirements 1.1-1.6, 2.4-2.6, 3.4, 3.9,
 * 8.1-8.5, 12.4).
 *
 * Availability = the deployed config names an assistant API, the state poll
 * says enabled, and the user is not DataLabeler-only. The drawer's open state
 * is remembered per browser; before the first toggle it is open on wide
 * viewports except on the workflow builder. The live Turn is polled every
 * second (slower while waiting for a confirmation or when the tab is hidden).
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react';
import type { ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { useOptionalUsecase } from '../../contexts/UsecaseContext';
import {
  assistantApi, assistantConfigured, isLive,
} from './assistantApi';
import type {
  ConfirmationCardData, ConversationSummary, PageContext, StoredMessage, TurnEvent, TurnFailure,
  TurnState, TurnStatus,
} from './assistantApi';
import {
  CONVERSATION_KEY, DRAWER_STATE_KEY, FIRST_VISIT_MIN_WIDTH, POLL_INTERVAL_HIDDEN_MS,
  POLL_INTERVAL_MS, POLL_INTERVAL_WAITING_MS, STATE_POLL_MS, WAITING_SLOWDOWN_MS,
} from './constants';

export interface LiveTurn {
  turnId: string;
  status: TurnStatus;
  version: number;
  prompt: string;
  text: string;
  events: TurnEvent[];
  confirmation: ConfirmationCardData | null;
  failure: TurnFailure | null;
  startedAt: number;
  waitingSince: number | null;
}

export interface AssistantContextValue {
  configured: boolean;
  available: boolean;
  actionsEnabled: boolean;
  unavailableReason: string | null;
  open: boolean;
  setOpen: (open: boolean) => void;
  unread: boolean;
  conversationId: string | null;
  messages: StoredMessage[];
  liveTurn: LiveTurn | null;
  conversations: ConversationSummary[];
  loading: boolean;
  error: string | null;
  draft: string;
  setDraft: (value: string) => void;
  send: (prompt: string) => Promise<void>;
  stop: () => Promise<void>;
  confirm: (bindingId: string, decision: 'confirm' | 'reject') => Promise<void>;
  newConversation: () => void;
  openConversation: (cid: string) => Promise<void>;
  deleteConversation: (cid: string) => Promise<void>;
  refreshConversations: () => Promise<void>;
  clearError: () => void;
  retryState: () => Promise<void>;
}

const AssistantContext = createContext<AssistantContextValue | undefined>(undefined);

export function useAssistant(): AssistantContextValue {
  const value = useContext(AssistantContext);
  if (!value) {
    throw new Error('useAssistant must be used within AssistantProvider');
  }
  return value;
}

/** The remembered drawer state, or the first-visit rule (Requirement 2.4). */
export function initialOpenState(route: string, remembered: string | null, viewportWidth: number): boolean {
  if (remembered === 'open') return true;
  if (remembered === 'closed') return false;
  if (route.startsWith('/workflows/builder')) return false;
  return viewportWidth >= FIRST_VISIT_MIN_WIDTH;
}

export function pollIntervalFor(turn: LiveTurn | null, hidden: boolean, now: number): number | null {
  if (!turn || !isLive(turn.status)) return null;
  if (hidden) return POLL_INTERVAL_HIDDEN_MS;
  if (turn.status === 'awaiting_confirmation' && turn.waitingSince !== null
      && now - turn.waitingSince > WAITING_SLOWDOWN_MS) {
    return POLL_INTERVAL_WAITING_MS;
  }
  return POLL_INTERVAL_MS;
}

function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const err = error as { message?: string; code?: string; details?: Record<string, unknown> };
    if (err.code === 'quota_exceeded') {
      const limit = String(err.details?.limit ?? 'usage').replace(/_/g, ' ');
      const resets = err.details?.resets_at ? ` It resets at ${new Date(String(err.details.resets_at)).toLocaleTimeString()}.` : '';
      return `You have reached the ${limit} limit.${resets}`;
    }
    if (err.code === 'conversation_full') return 'This conversation is full. Start a new one to continue.';
    if (err.code === 'assistant_disabled') return 'The assistant has been disabled by an administrator.';
    if (err.code === 'binding_expired') return 'This confirmation expired. Ask again to retry the action.';
    if (err.code === 'binding_used') return 'This confirmation was already answered.';
    return err.message || 'The assistant is unavailable right now.';
  }
  return 'The assistant is unavailable right now.';
}

function readStorage(storage: Storage | undefined, key: string): string | null {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function writeStorage(storage: Storage | undefined, key: string, value: string | null): void {
  try {
    if (value === null) storage?.removeItem(key);
    else storage?.setItem(key, value);
  } catch {
    /* storage unavailable: the state just is not remembered */
  }
}

export function AssistantProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { user } = useAuth();
  const selectedUsecaseId = useOptionalUsecase()?.selectedUsecaseId ?? null;
  const configured = assistantConfigured();
  const labelerOnly = user?.role === 'DataLabeler';

  const [enabled, setEnabled] = useState<boolean>(configured);
  const [actionsEnabled, setActionsEnabled] = useState(false);
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null);
  const [open, setOpenState] = useState<boolean>(() =>
    initialOpenState(
      location.pathname,
      readStorage(typeof window !== 'undefined' ? window.localStorage : undefined, DRAWER_STATE_KEY),
      typeof window !== 'undefined' ? window.innerWidth : 0,
    ),
  );
  const [unread, setUnread] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(() =>
    readStorage(typeof window !== 'undefined' ? window.sessionStorage : undefined, CONVERSATION_KEY));
  const [messages, setMessages] = useState<StoredMessage[]>([]);
  const [liveTurn, setLiveTurn] = useState<LiveTurn | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const openRef = useRef(open);
  openRef.current = open;

  const available = configured && enabled && !labelerOnly;

  // ------------------------------------------------------------ state poll
  const refreshState = useCallback(async () => {
    if (!configured || labelerOnly) return;
    try {
      const state = await assistantApi.state();
      setEnabled(state.enabled);
      setActionsEnabled(state.actions_enabled);
      setUnavailableReason(state.enabled ? null : state.reason ?? 'disabled_by_admin');
    } catch (err) {
      // a failing state read keeps the last known state; a 403 means off
      const code = (err as { code?: string }).code;
      if (code === 'assistant_disabled' || code === 'role_excluded') {
        setEnabled(false);
        setUnavailableReason(code);
      }
    }
  }, [configured, labelerOnly]);

  useEffect(() => {
    if (!configured || labelerOnly) return undefined;
    void refreshState();
    const timer = window.setInterval(() => { void refreshState(); }, STATE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [configured, labelerOnly, refreshState]);

  // ------------------------------------------------------------- drawer
  const setOpen = useCallback((next: boolean) => {
    setOpenState(next);
    writeStorage(window.localStorage, DRAWER_STATE_KEY, next ? 'open' : 'closed');
    if (next) setUnread(false);
  }, []);

  // ----------------------------------------------------- conversations
  const refreshConversations = useCallback(async () => {
    if (!available) return;
    try {
      const result = await assistantApi.listConversations(20);
      setConversations(result.items);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [available]);

  const applyTurnState = useCallback((state: TurnState, prompt: string, previous: LiveTurn | null): LiveTurn => {
    const now = Date.now();
    const waitingSince = state.status === 'awaiting_confirmation'
      ? (previous?.waitingSince ?? now) : null;
    return {
      turnId: state.turn_id,
      status: state.status,
      version: state.version,
      prompt,
      text: state.text ?? previous?.text ?? '',
      events: state.events ?? previous?.events ?? [],
      confirmation: state.confirmation ?? null,
      failure: state.failure ?? null,
      startedAt: previous?.startedAt ?? now,
      waitingSince,
    };
  }, []);

  const openConversation = useCallback(async (cid: string) => {
    if (!available) return;
    setLoading(true);
    setError(null);
    try {
      const detail = await assistantApi.getConversation(cid);
      setConversationId(cid);
      writeStorage(window.sessionStorage, CONVERSATION_KEY, cid);
      setMessages(detail.messages);
      if (detail.live_turn_id) {
        const state = await assistantApi.getTurn(cid, detail.live_turn_id);
        setLiveTurn(isLive(state.status) ? applyTurnState(state, state.prompt ?? '', null) : null);
      } else {
        setLiveTurn(null);
      }
    } catch (err) {
      const code = (err as { code?: string }).code;
      if (code === 'not_found') {
        setConversationId(null);
        writeStorage(window.sessionStorage, CONVERSATION_KEY, null);
        setMessages([]);
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setLoading(false);
    }
  }, [available, applyTurnState]);

  // reopen the remembered conversation once the assistant is available
  const reopened = useRef(false);
  useEffect(() => {
    if (!available || reopened.current) return;
    reopened.current = true;
    if (conversationId) void openConversation(conversationId);
    void refreshConversations();
  }, [available, conversationId, openConversation, refreshConversations]);

  const newConversation = useCallback(() => {
    setConversationId(null);
    writeStorage(window.sessionStorage, CONVERSATION_KEY, null);
    setMessages([]);
    setLiveTurn(null);
    setError(null);
  }, []);

  const deleteConversation = useCallback(async (cid: string) => {
    try {
      await assistantApi.deleteConversation(cid);
      if (cid === conversationId) newConversation();
      await refreshConversations();
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [conversationId, newConversation, refreshConversations]);

  // --------------------------------------------------------------- turns
  const pageContext = useCallback((): PageContext => {
    const params: Record<string, string> = {};
    const segments = location.pathname.split('/').filter(Boolean);
    // identifier-like trailing segments are useful context for the model
    if (segments.length >= 2) params.last_segment = segments[segments.length - 1].slice(0, 256);
    return { route: location.pathname, params, usecase_id: selectedUsecaseId ?? null };
  }, [location.pathname, selectedUsecaseId]);

  const send = useCallback(async (prompt: string) => {
    if (!available || liveTurn) return;
    const text = prompt.trim();
    if (!text) return;
    setError(null);
    setLoading(true);
    try {
      let cid = conversationId;
      if (!cid) {
        const created = await assistantApi.createConversation(text.slice(0, 80));
        cid = created.conversation_id;
        setConversationId(cid);
        writeStorage(window.sessionStorage, CONVERSATION_KEY, cid);
        setMessages([]);
      }
      const started = await assistantApi.startTurn(cid, text, pageContext());
      setLiveTurn({
        turnId: started.turn_id, status: 'queued', version: 0, prompt: text, text: '', events: [],
        confirmation: null, failure: null, startedAt: Date.now(), waitingSince: null,
      });
      setDraft('');
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [available, liveTurn, conversationId, pageContext]);

  const stop = useCallback(async () => {
    if (!conversationId || !liveTurn) return;
    try {
      await assistantApi.stop(conversationId, liveTurn.turnId);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [conversationId, liveTurn]);

  const confirm = useCallback(async (bindingId: string, decision: 'confirm' | 'reject') => {
    if (!conversationId || !liveTurn) return;
    try {
      await assistantApi.confirm(conversationId, liveTurn.turnId, bindingId, decision);
      setLiveTurn((previous) => previous ? { ...previous, status: 'running', confirmation: null, waitingSince: null } : previous);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [conversationId, liveTurn]);

  // the poll loop
  useEffect(() => {
    if (!conversationId || !liveTurn || !isLive(liveTurn.status)) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      if (cancelled) return;
      try {
        const state = await assistantApi.getTurn(conversationId, liveTurn.turnId, liveTurn.version);
        if (cancelled) return;
        if (state.version !== liveTurn.version || !isLive(state.status)) {
          const full = state.text === undefined
            ? await assistantApi.getTurn(conversationId, liveTurn.turnId)
            : state;
          if (cancelled) return;
          const next = applyTurnState(full, liveTurn.prompt, liveTurn);
          if (!isLive(next.status)) {
            // the Turn ended: fold it into the stored messages
            const detail = await assistantApi.getConversation(conversationId);
            if (cancelled) return;
            setMessages(detail.messages);
            setLiveTurn(null);
            if (!openRef.current) setUnread(true);
            void refreshConversations();
            return;
          }
          if (next.status === 'awaiting_confirmation' && liveTurn.status !== 'awaiting_confirmation'
              && !openRef.current) {
            setUnread(true);
          }
          setLiveTurn(next);
          return;
        }
      } catch (err) {
        const code = (err as { code?: string }).code;
        if (code === 'assistant_disabled' || code === 'role_excluded') {
          setEnabled(false);
          setUnavailableReason(code);
          return;
        }
        if (code === 'not_found') {
          setLiveTurn(null);
          return;
        }
        // transient: keep polling
      }
      const interval = pollIntervalFor(liveTurn, document.visibilityState === 'hidden', Date.now());
      if (interval !== null && !cancelled) timer = window.setTimeout(tick, interval);
    };
    const interval = pollIntervalFor(liveTurn, document.visibilityState === 'hidden', Date.now());
    timer = window.setTimeout(tick, interval ?? POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [conversationId, liveTurn, applyTurnState, refreshConversations]);

  const value = useMemo<AssistantContextValue>(() => ({
    configured,
    available,
    actionsEnabled,
    unavailableReason,
    open,
    setOpen,
    unread,
    conversationId,
    messages,
    liveTurn,
    conversations,
    loading,
    error,
    draft,
    setDraft,
    send,
    stop,
    confirm,
    newConversation,
    openConversation,
    deleteConversation,
    refreshConversations,
    clearError: () => setError(null),
    retryState: refreshState,
  }), [configured, available, actionsEnabled, unavailableReason, open, setOpen, unread, conversationId,
    messages, liveTurn, conversations, loading, error, draft, send, stop, confirm, newConversation,
    openConversation, deleteConversation, refreshConversations, refreshState]);

  return <AssistantContext.Provider value={value}>{children}</AssistantContext.Provider>;
}
