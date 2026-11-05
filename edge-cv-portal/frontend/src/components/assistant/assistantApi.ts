/**
 * The assistant API client (spec portal-ai-assistant, design §12).
 *
 * A `fetch` wrapper on `config.assistantApiUrl` with the Portal's ID token
 * and the shared one-shot 401 refresh (`handleUnauthorized` from
 * services/api.ts). Unlike `apiService.request`, it never drives the
 * app-wide loading bar: the Turn polls would keep it lit.
 */
import { getBuildInfo, getConfig } from '../../config';
import { ApiError, handleUnauthorized } from '../../services/api';

export type TurnStatus = 'queued' | 'running' | 'awaiting_confirmation' | 'completed' | 'stopped' | 'failed';

export interface AssistantState {
  enabled: boolean;
  actions_enabled: boolean;
  reason?: 'disabled_by_admin' | 'role_excluded';
  version: string;
}

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  created_at: number;
  updated_at: number;
  turn_count: number;
  live_turn_id: string | null;
}

export interface TurnEvent {
  seq: number;
  ts: number;
  type: string;
  text_offset: number;
  tool?: string;
  title?: string;
  args_summary?: string;
  category?: string | null;
  duration_ms?: number;
  status?: number;
  code?: string;
  limit?: string;
  decision?: string;
  binding_id?: string;
}

export interface ConfirmationCardData {
  binding_id: string;
  tool: string;
  title: string;
  description: string;
  args: Record<string, unknown>;
  usecase_id: string | null;
  destructive: boolean;
  expires_at: number;
}

export interface TurnFailure {
  code: string;
  message: string;
}

export interface TurnState {
  turn_id: string;
  conversation_id: string;
  status: TurnStatus;
  version: number;
  text?: string;
  events?: TurnEvent[];
  confirmation?: ConfirmationCardData | null;
  failure?: TurnFailure | null;
  prompt?: string;
  created_at?: number;
  finished_at?: number | null;
}

export interface DisplayBlock {
  type: 'user' | 'assistant';
  text: string;
  events?: TurnEvent[];
  status?: string;
  failure?: TurnFailure | null;
}

export interface StoredMessage {
  seq: number;
  role: 'user' | 'assistant';
  turn_id: string;
  created_at: number;
  blocks: DisplayBlock[];
}

export interface ConversationDetail {
  conversation_id: string;
  title: string;
  messages: StoredMessage[];
  live_turn_id: string | null;
  turn_count: number;
}

export interface AssistantSettingsView {
  enabled: boolean;
  actions_enabled: boolean;
  actions_delivered: boolean;
  /** Destructive catalog tools the assistant may propose (the Phase 2 allow-list; a code constant). */
  destructive_allowlist: string[];
  retention_days: number;
  guardrail_id: string | null;
  guardrail_version: string | null;
  model: { model_id: string; region: string; tool_use_supported: boolean | null; checked_at: number | null };
  limits: Record<string, number>;
  updated_at?: number;
  updated_by?: string;
  version: string;
}

export interface PageContext {
  route: string;
  params?: Record<string, string>;
  usecase_id?: string | null;
}

/** True when the deployed config names an assistant API. */
export function assistantConfigured(): boolean {
  const url = getConfig().assistantApiUrl;
  return typeof url === 'string' && url.trim().length > 0;
}

function baseUrl(): string {
  return (getConfig().assistantApiUrl ?? '').replace(/\/$/, '');
}

function headersWith(token: string | null): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Portal-Build': (getBuildInfo().gitSha || 'unknown').slice(0, 40),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const url = `${baseUrl()}${path}`;
  const token = localStorage.getItem('idToken');
  const init = (headers: Record<string, string>): RequestInit => ({
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let response = await fetch(url, init(headersWith(token)));
  if (response.status === 401) {
    const retried = await handleUnauthorized((fresh) => fetch(url, init(headersWith(fresh))));
    if (retried) response = retried;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = payload?.error;
    if (error && typeof error === 'object') {
      throw new ApiError(error.message || `HTTP ${response.status}`, response.status, error.code, error.details);
    }
    throw new ApiError(`HTTP ${response.status}`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const assistantApi = {
  state: () => request<AssistantState>('GET', '/assistant/state'),
  settings: () => request<AssistantSettingsView>('GET', '/assistant/settings'),
  updateSettings: (changes: Partial<Pick<AssistantSettingsView, 'enabled' | 'actions_enabled' | 'retention_days' | 'guardrail_id' | 'guardrail_version'>>) =>
    request<AssistantSettingsView>('PUT', '/assistant/settings', changes),
  listConversations: (limit = 20) =>
    request<{ items: ConversationSummary[] }>('GET', `/assistant/conversations?limit=${limit}`),
  createConversation: (title?: string) =>
    request<{ conversation_id: string }>('POST', '/assistant/conversations', title ? { title } : {}),
  getConversation: (cid: string) => request<ConversationDetail>('GET', `/assistant/conversations/${cid}`),
  deleteConversation: (cid: string) => request<void>('DELETE', `/assistant/conversations/${cid}`),
  startTurn: (cid: string, prompt: string, pageContext: PageContext) =>
    request<{ turn_id: string }>('POST', `/assistant/conversations/${cid}/turns`, {
      prompt,
      page_context: pageContext,
    }),
  getTurn: (cid: string, tid: string, after?: number) =>
    request<TurnState>('GET', `/assistant/conversations/${cid}/turns/${tid}${after !== undefined ? `?after=${after}` : ''}`),
  confirm: (cid: string, tid: string, bindingId: string, decision: 'confirm' | 'reject') =>
    request<{ turn_id: string; decision: string }>('POST', `/assistant/conversations/${cid}/turns/${tid}/confirmation`, {
      binding_id: bindingId,
      decision,
    }),
  stop: (cid: string, tid: string) =>
    request<{ turn_id: string; status: TurnStatus }>('POST', `/assistant/conversations/${cid}/turns/${tid}/stop`),
};

export function isLive(status: TurnStatus | undefined): boolean {
  return status === 'queued' || status === 'running' || status === 'awaiting_confirmation';
}
