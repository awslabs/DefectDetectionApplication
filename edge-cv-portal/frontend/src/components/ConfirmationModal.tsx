import {
  Modal,
  Box,
  SpaceBetween,
  Button,
  Alert,
} from '@cloudscape-design/components';

export interface ConfirmationModalProps {
  visible: boolean;
  title: string;
  message: string;
  confirmButtonText?: string;
  cancelButtonText?: string;
  variant?: 'danger' | 'warning' | 'info';
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children?: React.ReactNode;
}

export default function ConfirmationModal({
  visible,
  title,
  message,
  confirmButtonText = 'Confirm',
  cancelButtonText = 'Cancel',
  variant = 'warning',
  loading = false,
  onConfirm,
  onCancel,
  children,
}: ConfirmationModalProps) {
  const alertTypeMap = {
    danger: 'error' as const,
    warning: 'warning' as const,
    info: 'info' as const,
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onCancel}
      header={title}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onCancel} disabled={loading}>
              {cancelButtonText}
            </Button>
            <Button variant="primary" onClick={onConfirm} loading={loading}>
              {confirmButtonText}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Alert type={alertTypeMap[variant]}>{message}</Alert>
        {children}
      </SpaceBetween>
    </Modal>
  );
}
