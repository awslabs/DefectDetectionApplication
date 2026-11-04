/**
 * The Action_Confirmation card (spec portal-ai-assistant Requirement 5.4;
 * design §12): the proposed tool, every argument, the Use_Case, the
 * DESTRUCTIVE badge, the expiry, Confirm and Decline.
 */
import { useEffect, useState } from 'react';
import { Badge, Box, Button, Container, Header, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import type { ConfirmationCardData } from './assistantApi';

interface Props {
  card: ConfirmationCardData;
  onDecide: (decision: 'confirm' | 'reject') => void;
  disabled?: boolean;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

export default function ConfirmationCard({ card, onDecide, disabled }: Props) {
  const [remaining, setRemaining] = useState(() => Math.max(0, card.expires_at * 1000 - Date.now()));
  useEffect(() => {
    const timer = window.setInterval(() => {
      setRemaining(Math.max(0, card.expires_at * 1000 - Date.now()));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [card.expires_at]);
  const expired = remaining <= 0;
  const minutes = Math.floor(remaining / 60_000);
  const seconds = Math.floor((remaining % 60_000) / 1000);
  const entries = Object.entries(card.args ?? {}).sort(([a], [b]) => a.localeCompare(b));

  return (
    <Container
      data-testid="assistant-confirmation"
      header={
        <Header
          variant="h3"
          description={card.description}
          actions={card.destructive ? <Badge color="red">DESTRUCTIVE</Badge> : <Badge color="blue">CHANGE</Badge>}
        >
          Confirm: {card.title}
        </Header>
      }
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => onDecide('reject')} disabled={disabled || expired}>
              Decline
            </Button>
            <Button
              variant="primary"
              onClick={() => onDecide('confirm')}
              disabled={disabled || expired}
              data-testid="assistant-confirm"
            >
              {card.destructive ? `Confirm ${card.title.toLowerCase()}` : 'Confirm'}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          <Box variant="awsui-key-label">Use case</Box>
          <div data-testid="assistant-confirmation-usecase">{card.usecase_id ?? 'global'}</div>
        </Box>
        <Box>
          <Box variant="awsui-key-label">Tool</Box>
          <code>{card.tool}</code>
        </Box>
        <div role="table" aria-label="Arguments" data-testid="assistant-confirmation-args">
          {entries.length === 0 && <Box color="text-body-secondary">No arguments</Box>}
          {entries.map(([name, value]) => (
            <div role="row" key={name} style={{ display: 'flex', gap: 8, padding: '2px 0' }}>
              <div role="cell" style={{ minWidth: 140 }}><code>{name}</code></div>
              <div role="cell" style={{ wordBreak: 'break-all' }}>{formatValue(value)}</div>
            </div>
          ))}
        </div>
        {expired ? (
          <StatusIndicator type="warning">This confirmation expired. Ask again to retry the action.</StatusIndicator>
        ) : (
          <Box color="text-body-secondary" fontSize="body-s">
            Expires in {minutes}:{String(seconds).padStart(2, '0')}. Nothing happens until you confirm.
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}
