/**
 * vLLM Package & Publish section (vllm-package-publish-gui, task 6.1).
 *
 * Presentational Cloudscape section for the Model Detail page. All
 * displayed content renders from the `PanelState` produced by
 * `useVllmPublishController` — the pure derivation in `publishState.ts`
 * — so record-derived state, session banners, and action gating are
 * fully determined by (record, role, session).
 *
 * Layout inside one Container headed "Component Packaging & Publish":
 * error/success/pending/progress banners, the Package_Publish_Action
 * row with the publish-only retry button, the packaged-state table,
 * the published-state key-value pairs, and the re-publish confirmation
 * modal.
 *
 * Requirements: 1.1, 1.4, 1.5, 1.6, 1.7, 2.2, 2.5, 3.1, 3.3, 4.2, 4.3, 4.6.
 */

import { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  KeyValuePairs,
  Modal,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';

import { UserRole } from '../../types';
import {
  useVllmPublishController,
  VllmPublishModel,
  PolledModel,
} from './useVllmPublishController';
import { nextComponentVersion } from './publishState';

export interface VllmPackagePublishSectionProps {
  model: VllmPublishModel;
  role: UserRole | undefined;
  onModelUpdate: (model: PolledModel) => void;
}

/** The page's timestamp convention (ModelDetail.tsx `formatTimestamp`). */
function formatTimestamp(timestamp?: number): string {
  if (!timestamp) return 'N/A';
  return new Date(timestamp).toLocaleString();
}

/** Row shape of the packaged-state table (Req 4.2, 4.6). */
interface PackagedRow {
  target: string;
  status: string;
  error?: string;
}

/** Map a packaged entry status onto a StatusIndicator (Req 4.2, 4.6). */
function packagedStatusIndicator(status: string) {
  if (status === 'packaged') {
    return <StatusIndicator type="success">Packaged</StatusIndicator>;
  }
  if (status === 'failed') {
    return <StatusIndicator type="error">Failed</StatusIndicator>;
  }
  return <StatusIndicator type="in-progress">{status}</StatusIndicator>;
}

export default function VllmPackagePublishSection({
  model,
  role,
  onModelUpdate,
}: VllmPackagePublishSectionProps) {
  const { panel, activate, confirm, cancelConfirm, activatePublishRetry } =
    useVllmPublishController(model, role, onModelUpdate);

  // Presentation-only state: mirrors the session's `confirming` phase
  // (entered via activate(), left via confirm()/cancelConfirm()) and
  // tracks dismissal of the success banner.
  const [confirmVisible, setConfirmVisible] = useState(false);
  const [dismissedSuccess, setDismissedSuccess] = useState<string | null>(null);

  if (!panel.visible) {
    // Non-vLLM records render nothing (Req 1.3, 5.1, 5.4).
    return null;
  }

  const handleActivate = () => {
    setDismissedSuccess(null);
    activate();
    if (panel.action.requiresConfirmation) {
      // Re-publish gates on explicit confirmation (Req 1.7); the
      // reducer has entered `confirming` and emitted no invocation.
      setConfirmVisible(true);
    }
  };

  const handleConfirm = () => {
    setConfirmVisible(false);
    confirm();
  };

  const handleCancelConfirm = () => {
    setConfirmVisible(false);
    cancelConfirm();
  };

  const handlePublishRetry = () => {
    setDismissedSuccess(null);
    activatePublishRetry();
  };

  const showSuccess =
    panel.success !== undefined && panel.success.message !== dismissedSuccess;

  const publishedVersion = panel.publishedSection?.componentVersion;

  return (
    <Container
      data-testid="vllm-publish-section"
      header={
        <Header
          variant="h2"
          description="Package this vLLM model and publish it as a Greengrass component"
        >
          Component Packaging &amp; Publish
        </Header>
      }
    >
      <SpaceBetween size="m">
        {/* Session banners (Req 2.1, 2.2, 2.5, 3.1, 3.2, 4.6). */}
        {panel.error && (
          <Alert
            type="error"
            header="Packaging / publish failed"
            data-testid="vllm-publish-error"
          >
            <SpaceBetween size="xxs">
              <Box variant="span">{panel.error.message}</Box>
              {panel.error.failedStep && (
                <Box variant="span">Failed step: {panel.error.failedStep}</Box>
              )}
            </SpaceBetween>
          </Alert>
        )}

        {showSuccess && panel.success && (
          <Alert
            type="success"
            dismissible
            onDismiss={() => setDismissedSuccess(panel.success?.message ?? null)}
            data-testid="vllm-publish-success"
          >
            {panel.success.message}
          </Alert>
        )}

        {panel.pending && (
          <Alert type="warning" data-testid="vllm-publish-pending">
            {panel.pending.message}
          </Alert>
        )}

        {panel.progress && (
          <Alert type="info" data-testid="vllm-publish-progress">
            <StatusIndicator type="in-progress">
              {panel.progress.message}
            </StatusIndicator>
          </Alert>
        )}

        {/* Action row (Req 1.1, 1.4, 1.5, 1.6, 3.3). */}
        <SpaceBetween size="xs">
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="primary"
              loading={panel.action.loading}
              disabled={!panel.action.enabled}
              onClick={handleActivate}
              data-testid="vllm-publish-action"
            >
              {panel.action.label}
            </Button>
            {panel.publishRetry && (
              <Button
                loading={panel.publishRetry.loading}
                disabled={!panel.publishRetry.enabled}
                onClick={handlePublishRetry}
                data-testid="vllm-publish-retry"
              >
                Publish packaged component
              </Button>
            )}
          </SpaceBetween>
          {panel.action.permissionMessage && (
            <Box
              fontSize="body-s"
              color="text-status-inactive"
              data-testid="vllm-publish-permission-message"
            >
              {panel.action.permissionMessage}
            </Box>
          )}
          {!panel.action.permissionMessage && panel.action.republishNote && (
            <Box
              fontSize="body-s"
              color="text-status-inactive"
              data-testid="vllm-publish-republish-note"
            >
              {panel.action.republishNote}
            </Box>
          )}
        </SpaceBetween>

        {/* Packaged state (Req 4.1, 4.2, 4.6). */}
        {panel.packagedSection && (
          <Table<PackagedRow>
            data-testid="vllm-packaged-table"
            resizableColumns
            header={<Header variant="h3">Packaged Components</Header>}
            items={panel.packagedSection}
            columnDefinitions={[
              {
                id: 'target',
                header: 'Target',
                cell: (item) => item.target,
              },
              {
                id: 'status',
                header: 'Status',
                cell: (item) => packagedStatusIndicator(item.status),
              },
              {
                id: 'error',
                header: 'Error',
                cell: (item) =>
                  item.error ? (
                    <Box fontSize="body-s">{item.error}</Box>
                  ) : (
                    <Box fontSize="body-s" color="text-status-inactive">
                      N/A
                    </Box>
                  ),
              },
            ]}
            empty={<Box textAlign="center">No packaged components</Box>}
          />
        )}

        {/* Published state (Req 4.1, 4.3). */}
        {panel.publishedSection && (
          <Box data-testid="vllm-published-section">
            <KeyValuePairs
              columns={3}
              items={[
                {
                  label: 'Component Name',
                  value: panel.publishedSection.componentName,
                },
                {
                  label: 'Component Version',
                  value: panel.publishedSection.componentVersion,
                },
                {
                  label: 'Published',
                  value: formatTimestamp(panel.publishedSection.publishedAt),
                },
                ...Object.entries(panel.publishedSection.componentArns).map(
                  ([target, arn]) => ({
                    label: `Component ARN (${target})`,
                    value: (
                      <Box fontSize="body-s">
                        <span style={{ fontFamily: 'monospace' }}>{arn}</span>
                      </Box>
                    ),
                  })
                ),
              ]}
            />
          </Box>
        )}

        {/* Re-publish confirmation (Req 1.7). */}
        <Modal
          visible={confirmVisible}
          onDismiss={handleCancelConfirm}
          header="Re-publish Component"
          data-testid="vllm-republish-modal"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={handleCancelConfirm}
                  data-testid="vllm-republish-cancel"
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleConfirm}
                  data-testid="vllm-republish-confirm"
                >
                  Re-publish
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Alert type="warning">
            Re-publishing registers the next component version
            {publishedVersion
              ? ` (${nextComponentVersion(publishedVersion)})`
              : ''}
            . Continue?
          </Alert>
        </Modal>
      </SpaceBetween>
    </Container>
  );
}
