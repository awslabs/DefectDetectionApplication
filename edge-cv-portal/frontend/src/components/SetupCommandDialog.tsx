/**
 * SetupCommandDialog (station-quick-setup Requirement 2.4).
 *
 * Shown after a device registration succeeds. It displays the generated
 * one-line Setup_Command in monospace and offers a single-action
 * copy-to-clipboard control that places the complete command on the
 * clipboard in one click (Cloudscape's CopyToClipboard copies exactly the
 * `textToCopy` string). It also shows the Setup_Token expiration as a
 * human-readable local date and time so the operator knows how long the
 * command stays valid.
 *
 * The dialog is presentational: the command string and expiry are supplied
 * by the caller (the registration response carries `setup_command` and
 * `token_expires_at`); this component never fetches or mutates state.
 */
import {
  Box,
  Button,
  CopyToClipboard,
  KeyValuePairs,
  Modal,
  SpaceBetween,
} from '@cloudscape-design/components';

export interface SetupCommandDialogProps {
  /** The complete one-line Setup_Command to display and copy. */
  setupCommand: string;
  /**
   * Setup_Token expiration time in epoch seconds (the registration
   * response's `token_expires_at`).
   */
  tokenExpiresAt: number;
  /** Optional device name, used only to title the dialog. */
  deviceName?: string;
  onDismiss: () => void;
}

/**
 * Format an epoch-seconds expiration as a local date and time string,
 * matching the portal's `new Date(...).toLocaleString()` convention.
 */
export function formatExpiration(tokenExpiresAt: number): string {
  return new Date(tokenExpiresAt * 1000).toLocaleString();
}

export default function SetupCommandDialog({
  setupCommand,
  tokenExpiresAt,
  deviceName,
  onDismiss,
}: SetupCommandDialogProps) {
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={deviceName ? `Setup command for ${deviceName}` : 'Setup command'}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Done
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="l">
        <Box variant="p">
          Run this command on the station to provision it. It downloads and
          runs the installer with no repository checkout or AWS login
          required. The command is valid until the setup token expires.
        </Box>

        <SpaceBetween size="xs">
          <Box variant="awsui-key-label">Setup command</Box>
          {/* Monospace rendering of the complete command (Requirement 2.4). */}
          <Box variant="code" display="block">
            {setupCommand}
          </Box>
          {/* Single-action copy control: one click copies the entire
              command string to the clipboard (Requirement 2.4). */}
          <CopyToClipboard
            variant="button"
            copyButtonText="Copy command"
            textToCopy={setupCommand}
            copyButtonAriaLabel="Copy the complete setup command to the clipboard"
            copySuccessText="Setup command copied"
            copyErrorText="Failed to copy the setup command"
          />
        </SpaceBetween>

        {/* Token expiration date and time (Requirement 2.4). */}
        <KeyValuePairs
          columns={1}
          items={[
            {
              label: 'Token expires',
              value: formatExpiration(tokenExpiresAt),
            },
          ]}
        />
      </SpaceBetween>
    </Modal>
  );
}
