/**
 * Recent conversations with reopen and delete (spec portal-ai-assistant
 * Requirement 9.2).
 */
import { useState } from 'react';
import { Box, Button, SpaceBetween } from '@cloudscape-design/components';
import ConfirmationModal from '../ConfirmationModal';
import type { ConversationSummary } from './assistantApi';

interface Props {
  conversations: ConversationSummary[];
  currentId: string | null;
  onOpen: (cid: string) => void;
  onDelete: (cid: string) => Promise<void>;
}

export default function ConversationList({ conversations, currentId, onOpen, onDelete }: Props) {
  const [pendingDelete, setPendingDelete] = useState<ConversationSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  if (conversations.length === 0) {
    return <Box color="text-body-secondary">No earlier conversations.</Box>;
  }
  return (
    <SpaceBetween size="xs">
      {conversations.map((conversation) => (
        <div
          key={conversation.conversation_id}
          data-testid="assistant-conversation-row"
          style={{ display: 'flex', alignItems: 'center', gap: 8 }}
        >
          <Button
            variant={conversation.conversation_id === currentId ? 'primary' : 'link'}
            onClick={() => onOpen(conversation.conversation_id)}
          >
            {conversation.title || 'Untitled conversation'}
          </Button>
          <Box color="text-body-secondary" fontSize="body-s">
            {new Date(conversation.updated_at).toLocaleString()}
          </Box>
          <Button
            variant="icon"
            iconName="remove"
            ariaLabel={`Delete conversation ${conversation.title || conversation.conversation_id}`}
            onClick={() => setPendingDelete(conversation)}
          />
        </div>
      ))}
      <ConfirmationModal
        visible={pendingDelete !== null}
        title="Delete conversation"
        message={`Delete "${pendingDelete?.title || 'this conversation'}"? Its messages are removed immediately.`}
        confirmButtonText="Delete"
        variant="danger"
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={async () => {
          if (!pendingDelete) return;
          setDeleting(true);
          try {
            await onDelete(pendingDelete.conversation_id);
          } finally {
            setDeleting(false);
            setPendingDelete(null);
          }
        }}
      />
    </SpaceBetween>
  );
}
