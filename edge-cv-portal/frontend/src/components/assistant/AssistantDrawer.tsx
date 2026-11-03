/**
 * The assistant panel (spec portal-ai-assistant Requirements 2.7, 2.8, 3,
 * 5.3-5.4, 8.1-8.5, 9.2, 11.1, 11.4, 13.6; design §12, §13).
 *
 * Message log in a labelled region with a hidden live region, the live
 * Turn rendered from its text and events, the confirmation card, a pinned
 * composer with Stop while a Turn runs, suggested prompts on an empty
 * conversation, the disclaimer, and the error and unavailable states.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  Alert, Box, Button, ExpandableSection, Header, PromptInput, SpaceBetween, StatusIndicator,
} from '@cloudscape-design/components';
import { Avatar, ChatBubble, LoadingBar, SupportPromptGroup } from '@cloudscape-design/chat-components';
import { useAuth } from '../../contexts/AuthContext';
import ActivityTrace from './ActivityTrace';
import AssistantErrorBoundary from './AssistantErrorBoundary';
import { useAssistant } from './AssistantProvider';
import type { LiveTurn } from './AssistantProvider';
import ConfirmationCard from './ConfirmationCard';
import ConversationList from './ConversationList';
import MessageMarkdown from './MessageMarkdown';
import { DISCLAIMER, PROMPT_MAX_CHARS } from './constants';
import { suggestedPrompts } from './suggestedPrompts';
import type { StoredMessage, TurnEvent, TurnFailure } from './assistantApi';

const FAILURE_TEXT: Record<string, string> = {
  bedrock_throttled: 'The model is busy (throttled). Try again in a moment.',
  bedrock_authorization: 'The Portal is not allowed to call the configured model. Ask an administrator.',
  bedrock_model_access: 'The configured model is not available in this account or region. Ask an administrator.',
  bedrock_model_error: 'The model returned an error. Try again.',
  bedrock_timeout: 'The model did not answer within the configured timeout.',
  worker_lost: 'The assistant did not finish. Try again.',
  worker_unavailable: 'The assistant could not start. Try again.',
  not_authenticated: 'Your session expired. Sign in again to continue.',
  assistant_disabled: 'The assistant was disabled by an administrator.',
  binding_invalid: 'The confirmation did not match the proposal, so nothing was done.',
  internal_error: 'The assistant hit an internal error. Try again.',
};

export function failureText(failure: TurnFailure | null | undefined): string | null {
  if (!failure) return null;
  const known = FAILURE_TEXT[failure.code];
  if (failure.code === 'bedrock_timeout' && /\d+/.test(failure.message)) {
    return `The model did not answer within ${failure.message.match(/\d+/)?.[0]} seconds.`;
  }
  return known ?? failure.message ?? 'The assistant failed.';
}

function AssistantAvatar({ loading = false }: { loading?: boolean }) {
  return <Avatar color="gen-ai" iconName="gen-ai" ariaLabel="Assistant" tooltipText="Assistant" loading={loading} />;
}

function UserAvatar({ initials }: { initials: string }) {
  return <Avatar ariaLabel="You" tooltipText="You" initials={initials} />;
}

function initialsOf(name: string | undefined): string {
  const base = (name || 'U').replace(/@.*/, '');
  return base.slice(0, 2).toUpperCase();
}

function AssistantBubble({ text, events, status, failure, live, initials }: {
  text: string; events: TurnEvent[]; status?: string; failure?: TurnFailure | null; live?: boolean; initials: string;
}) {
  void initials;
  const failed = failureText(failure);
  return (
    <ChatBubble type="incoming" avatar={<AssistantAvatar loading={live} />} ariaLabel="Assistant response"
      showLoadingBar={live && !text}>
      <SpaceBetween size="xs">
        <ActivityTrace events={events} />
        {text ? <MessageMarkdown text={text} /> : null}
        {status === 'stopped' && <StatusIndicator type="stopped">Stopped</StatusIndicator>}
        {failed && <Alert type="error">{failed}</Alert>}
      </SpaceBetween>
    </ChatBubble>
  );
}

function StoredMessages({ messages, initials }: { messages: StoredMessage[]; initials: string }) {
  return (
    <>
      {messages.map((message) => (
        <div key={message.seq} data-testid={`assistant-message-${message.role}`}>
          {message.blocks.map((block, index) => (
            block.type === 'user' ? (
              <ChatBubble key={index} type="outgoing" avatar={<UserAvatar initials={initials} />} ariaLabel="Your message">
                <Box>{block.text}</Box>
              </ChatBubble>
            ) : (
              <AssistantBubble key={index} text={block.text} events={block.events ?? []} status={block.status}
                failure={block.failure} initials={initials} />
            )
          ))}
        </div>
      ))}
    </>
  );
}

function LiveTurnView({ turn, initials, onDecide, deciding }: {
  turn: LiveTurn; initials: string; onDecide: (bindingId: string, decision: 'confirm' | 'reject') => void; deciding: boolean;
}) {
  const live = turn.status === 'queued' || turn.status === 'running';
  return (
    <div data-testid="assistant-live-turn">
      <ChatBubble type="outgoing" avatar={<UserAvatar initials={initials} />} ariaLabel="Your message">
        <Box>{turn.prompt}</Box>
      </ChatBubble>
      <AssistantBubble text={turn.text} events={turn.events} status={turn.status} failure={turn.failure}
        live={live} initials={initials} />
      {turn.status === 'awaiting_confirmation' && turn.confirmation && (
        <ConfirmationCard card={turn.confirmation} onDecide={(decision) => onDecide(turn.confirmation!.binding_id, decision)}
          disabled={deciding} />
      )}
      {live && <LoadingBar variant="gen-ai" />}
    </div>
  );
}

function DrawerBody() {
  const assistant = useAssistant();
  const { user } = useAuth();
  const location = useLocation();
  const [showHistory, setShowHistory] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const initials = initialsOf(user?.email || user?.username);
  const liveTurn = assistant.liveTurn;
  const busy = liveTurn !== null && (liveTurn.status === 'queued' || liveTurn.status === 'running');
  const canSend = assistant.available && !assistant.loading && liveTurn === null && assistant.draft.trim().length > 0;

  // announce the newest completed response (Requirement 2.8)
  const lastAssistant = useMemo(() => {
    for (let i = assistant.messages.length - 1; i >= 0; i -= 1) {
      const message = assistant.messages[i];
      if (message.role === 'assistant') return message.blocks[0]?.text ?? '';
    }
    return '';
  }, [assistant.messages]);

  useEffect(() => {
    // keep the newest content in view while a Turn streams
    logRef.current?.scrollTo?.({ top: logRef.current.scrollHeight });
  }, [assistant.messages.length, liveTurn?.version]);

  if (!assistant.available) {
    return (
      <Box padding="m">
        <Alert type="info" header="The assistant is unavailable">
          {assistant.unavailableReason === 'disabled_by_admin'
            ? 'An administrator has turned the assistant off.'
            : 'The assistant is not available for your account.'}
        </Alert>
      </Box>
    );
  }

  const prompts = suggestedPrompts(location.pathname);
  const empty = assistant.messages.length === 0 && liveTurn === null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}>
      <Box padding={{ horizontal: 'm', top: 's' }}>
        <Header
          variant="h3"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="history" variant="icon" ariaLabel="Earlier conversations"
                onClick={() => { setShowHistory((v) => !v); void assistant.refreshConversations(); }} />
              <Button iconName="add-plus" variant="icon" ariaLabel="New conversation"
                onClick={() => assistant.newConversation()} disabled={busy} />
            </SpaceBetween>
          }
        >
          Assistant
        </Header>
      </Box>
      {showHistory && (
        <Box padding={{ horizontal: 'm' }}>
          <ExpandableSection headerText="Earlier conversations" defaultExpanded>
            <ConversationList
              conversations={assistant.conversations}
              currentId={assistant.conversationId}
              onOpen={(cid) => { void assistant.openConversation(cid); setShowHistory(false); }}
              onDelete={assistant.deleteConversation}
            />
          </ExpandableSection>
        </Box>
      )}
      <div
        ref={logRef}
        role="region"
        aria-label="Assistant conversation"
        data-testid="assistant-log"
        style={{ flex: 1, overflowY: 'auto', padding: '0 16px' }}
      >
        <SpaceBetween size="m">
          {empty && (
            <Box color="text-body-secondary">
              Ask about this page, what to do next, or something to look up for you.
            </Box>
          )}
          <StoredMessages messages={assistant.messages} initials={initials} />
          {liveTurn && (
            <LiveTurnView
              turn={liveTurn}
              initials={initials}
              deciding={deciding}
              onDecide={(bindingId, decision) => {
                setDeciding(true);
                void assistant.confirm(bindingId, decision).finally(() => setDeciding(false));
              }}
            />
          )}
          {empty && (
            <SupportPromptGroup
              ariaLabel="Suggested prompts"
              alignment="vertical"
              items={prompts.map((text, index) => ({ id: `p${index}`, text }))}
              onItemClick={({ detail }) => {
                const text = prompts[Number(detail.id.slice(1))];
                if (text) void assistant.send(text);
              }}
            />
          )}
        </SpaceBetween>
        <div aria-live="polite" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>
          {lastAssistant}
        </div>
      </div>
      <Box padding="m">
        <SpaceBetween size="xs">
          {assistant.error && (
            <Alert type="error" dismissible onDismiss={assistant.clearError}
              action={<Button onClick={() => void assistant.retryState()}>Retry</Button>}>
              {assistant.error}
            </Alert>
          )}
          <PromptInput
            value={assistant.draft}
            onChange={({ detail }) => assistant.setDraft(detail.value.slice(0, PROMPT_MAX_CHARS))}
            onAction={() => { if (canSend) void assistant.send(assistant.draft); }}
            placeholder="Ask a question"
            ariaLabel="Ask the assistant"
            actionButtonIconName="send"
            actionButtonAriaLabel="Send"
            disableActionButton={!canSend}
            disabled={busy || !assistant.available}
            minRows={1}
            maxRows={6}
            secondaryActions={busy ? (
              <Button iconName="status-stopped" onClick={() => void assistant.stop()} data-testid="assistant-stop">
                Stop
              </Button>
            ) : undefined}
          />
          <Box color="text-body-secondary" fontSize="body-s">{DISCLAIMER}</Box>
        </SpaceBetween>
      </Box>
    </div>
  );
}

export default function AssistantDrawer() {
  return (
    <AssistantErrorBoundary>
      <DrawerBody />
    </AssistantErrorBoundary>
  );
}
