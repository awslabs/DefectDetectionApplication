/**
 * Build server Fleet page (portal-build-fleet-and-workflow-gates
 * Requirements 6.1, 6.2, 6.3, 6.6, 6.9, 6.12).
 *
 * PortalAdmin-gated like UserManager: the route lives inside the
 * authenticated layout, and signed-in users without the PortalAdmin role
 * see an access-denied notice and no fleet content (Requirement 6.7,
 * mirroring the UserManager "Portal Admin access required" pattern).
 *
 * The page lists every Dedicated_Build_Server with its name, instance
 * identifier, instance type, CPU architecture, lifecycle state, the
 * running Build_Job when one exists (linked to the job detail), and the
 * time of the last state change (Requirement 6.1). The list is polled
 * every 15 seconds so each lifecycle state transition is displayed well
 * within the 30-second refresh bound (Requirements 6.2, 6.3, 6.9);
 * background polls never blank the table — servers are only replaced on
 * a successful fetch.
 *
 * Actions are offered through the table header on the selected server:
 * Start is enabled only for a stopped server, Stop only for a running
 * server with no running Build_Job, and Terminate opens a type-the-name
 * confirmation modal — the terminate request is only sent when the typed
 * text matches the server name exactly, and cancelling or dismissing the
 * modal submits nothing, leaving the server unchanged (Requirements 6.6,
 * 6.12). Launch opens a modal with a server name field and a CPU
 * architecture radio selection (Requirement 6.5).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Flashbar,
  FormField,
  Header,
  Input,
  Link,
  Modal,
  RadioGroup,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import type {
  FlashbarProps,
  StatusIndicatorProps,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import type {
  BuildServer,
  BuildServerArchitecture,
  BuildServerUbuntuVersion,
} from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { getErrorMessage } from '../../utils/errorHandling';

/** Fleet list poll interval: 15 s, half the 30 s bound of Req 6.9. */
export const FLEET_POLL_INTERVAL_MS = 15_000;

/**
 * Pure action-enablement rules mirroring the backend
 * `validate_fleet_action` table (Requirements 6.2, 6.3, 6.4, 6.10):
 * start iff stopped; stop iff running with no running Build_Job;
 * terminate iff not terminated and no running Build_Job.
 */
export function canStartServer(server: BuildServer): boolean {
  return server.lifecycle_state === 'stopped';
}

export function canStopServer(server: BuildServer): boolean {
  return (
    server.lifecycle_state === 'running' && !server.running_build_job_id
  );
}

export function canTerminateServer(server: BuildServer): boolean {
  return (
    server.lifecycle_state !== 'terminated' && !server.running_build_job_id
  );
}

/** StatusIndicator type per lifecycle state (Requirement 6.1). */
export function lifecycleIndicatorType(
  state: BuildServer['lifecycle_state']
): StatusIndicatorProps.Type {
  switch (state) {
    case 'running':
      return 'success';
    case 'pending':
    case 'stopping':
    case 'shutting-down':
      return 'in-progress';
    case 'stopped':
    case 'terminated':
      return 'stopped';
    default:
      return 'info';
  }
}

/** Epoch-milliseconds timestamp rendered for display; '-' when absent. */
export function formatStateChange(epochMs?: number | null): string {
  if (!epochMs) return '-';
  return new Date(epochMs).toLocaleString();
}

interface LaunchModalProps {
  /** Called with the flashbar confirmation text on success. */
  onSuccess: (message: string) => void;
  onDismiss: () => void;
}

/**
 * Launch modal: server name field plus a CPU architecture radio
 * selection with no default — the admin must pick ARM64 or x86_64
 * before submission is possible (Requirement 6.5). ARM64 additionally
 * offers the Ubuntu version: 22.04 (default) or 24.04, the JetPack 7
 * (JP7) build host (jetpack7-support design §10); the choice is
 * arm64-only, so switching to x86_64 resets it to 22.04.
 */
export function LaunchServerModal({ onSuccess, onDismiss }: LaunchModalProps) {
  const [name, setName] = useState('');
  const [architecture, setArchitecture] = useState<
    '' | BuildServerArchitecture
  >('');
  const [ubuntuVersion, setUbuntuVersion] =
    useState<BuildServerUbuntuVersion>('22.04');
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  const isValid = name.trim().length > 0 && architecture !== '';

  const submit = async () => {
    if (!isValid || submitting) {
      return;
    }
    setServerError('');
    setSubmitting(true);
    try {
      await apiService.launchBuildServer({
        name: name.trim(),
        architecture: architecture as BuildServerArchitecture,
        ubuntu_version: ubuntuVersion,
      });
      onSuccess(
        `Build server ${name.trim()} is launching. The build environment ` +
          `is installed automatically; the server appears as running once ` +
          `ready.`
      );
    } catch (err: unknown) {
      setServerError(getErrorMessage(err, 'The server was not launched'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header="Launch build server"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={!isValid}
            >
              Launch server
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {serverError && (
          <Alert type="error" header="Launch failed">
            {serverError}
          </Alert>
        )}
        <FormField
          label="Server name"
          description="Shown in the fleet list and required to confirm termination."
        >
          <Input
            value={name}
            onChange={({ detail }) => setName(detail.value)}
            ariaLabel="Server name"
          />
        </FormField>
        <FormField
          label="CPU architecture"
          description="The instance type and volume size come from the build configuration."
        >
          <RadioGroup
            value={architecture || null}
            onChange={({ detail }) => {
              const nextArchitecture =
                detail.value as BuildServerArchitecture;
              setArchitecture(nextArchitecture);
              if (nextArchitecture !== 'arm64') {
                // 24.04 is arm64-only (JP7): leaving ARM64 restores the
                // 22.04 default the backend applies for every other
                // architecture.
                setUbuntuVersion('22.04');
              }
            }}
            items={[
              {
                value: 'arm64',
                label: 'ARM64',
                description:
                  'For ARM64 edge component builds (e.g. JP5, JP6, JP7).',
              },
              {
                value: 'x86_64',
                label: 'x86_64',
                description:
                  'For x86_64 edge component builds (e.g. AMD64, AMD64 NVIDIA).',
              },
            ]}
          />
        </FormField>
        {architecture === 'arm64' && (
          <FormField
            label="Ubuntu version"
            description="JetPack 7 (JP7) builds require an Ubuntu 24.04 host."
          >
            <RadioGroup
              value={ubuntuVersion}
              onChange={({ detail }) =>
                setUbuntuVersion(detail.value as BuildServerUbuntuVersion)
              }
              items={[
                {
                  value: '22.04',
                  label: 'Ubuntu 22.04 (default)',
                  description: 'For JP5 and JP6 builds.',
                },
                {
                  value: '24.04',
                  label: 'Ubuntu 24.04',
                  description: 'For JetPack 7 (JP7) builds.',
                },
              ]}
            />
          </FormField>
        )}
      </SpaceBetween>
    </Modal>
  );
}

interface TerminateModalProps {
  server: BuildServer;
  /** Called with the flashbar confirmation text on success. */
  onSuccess: (message: string) => void;
  onDismiss: () => void;
}

/**
 * Terminate confirmation modal (Requirements 6.6, 6.12): the terminate
 * request is only submittable when the typed text matches the server
 * name exactly (the backend re-verifies the echo); cancelling or
 * dismissing the modal performs no termination and leaves the server
 * unchanged.
 */
export function TerminateServerModal({
  server,
  onSuccess,
  onDismiss,
}: TerminateModalProps) {
  const [confirmText, setConfirmText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState('');

  // The explicit confirmation of Req 6.6: an exact name match.
  const confirmed = confirmText === server.name;

  const submit = async () => {
    if (!confirmed || submitting) {
      return;
    }
    setServerError('');
    setSubmitting(true);
    try {
      await apiService.terminateBuildServer(server.server_id, confirmText);
      onSuccess(`Build server ${server.name} is terminating.`);
    } catch (err: unknown) {
      setServerError(getErrorMessage(err, 'The server was not terminated'));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible
      // Dismissing submits nothing: the server is left unchanged (6.12).
      onDismiss={onDismiss}
      header={`Terminate build server ${server.name}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={!confirmed}
            >
              Terminate server
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {serverError && (
          <Alert type="error" header="Termination failed">
            {serverError}
          </Alert>
        )}
        <Alert type="warning">
          Terminating permanently destroys the instance{' '}
          <b>{server.instance_id}</b> and its build workspace. This cannot
          be undone.
        </Alert>
        <FormField
          label={
            <>
              Type the server name <b>{server.name}</b> to confirm
            </>
          }
        >
          <Input
            value={confirmText}
            onChange={({ detail }) => setConfirmText(detail.value)}
            placeholder={server.name}
            ariaLabel="Confirm server name"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}

/** Which action modal is open, if any. */
type ActiveModal = 'launch' | 'terminate' | null;

export default function FleetPage() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';
  const navigate = useNavigate();

  const [servers, setServers] = useState<BuildServer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [selectedServerId, setSelectedServerId] = useState<string | null>(
    null
  );
  const [activeModal, setActiveModal] = useState<ActiveModal>(null);
  const [flashItems, setFlashItems] = useState<
    FlashbarProps.MessageDefinition[]
  >([]);
  // Per-server start/stop in-flight marker so the buttons show a spinner
  // and cannot be double-submitted.
  const [actionInFlight, setActionInFlight] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  /**
   * Fetch the fleet list. The initial load shows the table spinner; the
   * 15 s background polls (Requirement 6.9) replace the servers only on
   * success, so a transient poll failure never blanks the table — the
   * error is surfaced in an alert above it instead.
   */
  const loadServers = useCallback(async (initial = false) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    if (initial) {
      setLoading(true);
    }
    try {
      const response = await apiService.listBuildServers();
      setServers(response.servers ?? []);
      setError('');
      setLoaded(true);
    } catch (err: unknown) {
      setError(getErrorMessage(err, 'Failed to load build servers'));
    } finally {
      inFlightRef.current = false;
      if (initial) {
        setLoading(false);
      }
    }
  }, []);

  // Poll every 15 s so every lifecycle state transition is displayed
  // within the 30 s bound (Requirements 6.2, 6.3, 6.9).
  useEffect(() => {
    if (!isPortalAdmin) return;
    loadServers(true);
    const timer = setInterval(() => loadServers(), FLEET_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [isPortalAdmin, loadServers]);

  const pushFlash = (
    message: string,
    type: 'success' | 'error' = 'success'
  ) => {
    const id = `fleet-flash-${Date.now()}-${Math.random()}`;
    setFlashItems((items) => [
      ...items,
      {
        id,
        type,
        content: message,
        dismissible: true,
        onDismiss: () =>
          setFlashItems((current) =>
            current.filter((item) => item.id !== id)
          ),
      },
    ]);
  };

  /** Shared success path of the launch and terminate modals. */
  const handleActionSuccess = (message: string) => {
    setActiveModal(null);
    pushFlash(message);
    loadServers();
  };

  /**
   * Start/stop the selected server (Requirements 6.2, 6.3). The
   * transition is confirmed by the immediate re-fetch and followed by
   * the 15 s polling until the target state is reached; rejections
   * (e.g. a state change between polls, Requirement 6.10) surface the
   * backend's error naming the current lifecycle state.
   */
  const runLifecycleAction = async (
    server: BuildServer,
    action: 'start' | 'stop'
  ) => {
    setActionInFlight(server.server_id);
    try {
      if (action === 'start') {
        await apiService.startBuildServer(server.server_id);
        pushFlash(`Build server ${server.name} is starting.`);
      } else {
        await apiService.stopBuildServer(server.server_id);
        pushFlash(`Build server ${server.name} is stopping.`);
      }
      loadServers();
    } catch (err: unknown) {
      pushFlash(
        getErrorMessage(err, `The ${action} action failed on ${server.name}`),
        'error'
      );
    } finally {
      setActionInFlight(null);
    }
  };

  if (!isPortalAdmin) {
    // Access-denied notice only — no fleet content (Requirement 6.7),
    // mirroring the UserManager gating pattern.
    return (
      <Alert type="info" header="Portal Admin access required">
        Build servers can only be viewed and managed by Portal Admins.
      </Alert>
    );
  }

  const selectedServer =
    servers.find((server) => server.server_id === selectedServerId) ?? null;
  const selectedItems = selectedServer ? [selectedServer] : [];

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Launch, start, stop, and terminate dedicated build servers"
      >
        Build Server Fleet
      </Header>

      {flashItems.length > 0 && <Flashbar items={flashItems} />}

      {error && (
        <Alert
          type="error"
          header="Failed to load build servers"
          action={<Button onClick={() => loadServers(true)}>Retry</Button>}
        >
          {error}
        </Alert>
      )}

      <Table
        header={
          <Header
            variant="h2"
            counter={`(${servers.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={() => loadServers()}>
                  Refresh
                </Button>
                <Button
                  // Start iff the server is stopped (Requirements 6.2, 6.10).
                  disabled={!selectedServer || !canStartServer(selectedServer)}
                  loading={
                    !!selectedServer &&
                    actionInFlight === selectedServer.server_id
                  }
                  onClick={() =>
                    selectedServer && runLifecycleAction(selectedServer, 'start')
                  }
                >
                  Start
                </Button>
                <Button
                  // Stop iff running with no running Build_Job
                  // (Requirements 6.3, 6.4, 6.10).
                  disabled={!selectedServer || !canStopServer(selectedServer)}
                  loading={
                    !!selectedServer &&
                    actionInFlight === selectedServer.server_id
                  }
                  onClick={() =>
                    selectedServer && runLifecycleAction(selectedServer, 'stop')
                  }
                >
                  Stop
                </Button>
                <Button
                  disabled={
                    !selectedServer || !canTerminateServer(selectedServer)
                  }
                  onClick={() => setActiveModal('terminate')}
                >
                  Terminate
                </Button>
                <Button
                  variant="primary"
                  onClick={() => setActiveModal('launch')}
                >
                  Launch server
                </Button>
              </SpaceBetween>
            }
          >
            Build servers
          </Header>
        }
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) =>
          setSelectedServerId(detail.selectedItems[0]?.server_id ?? null)
        }
        trackBy="server_id"
        ariaLabels={{
          selectionGroupLabel: 'Server selection',
          itemSelectionLabel: (_data, row) => `Select ${row.name}`,
        }}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: (item) => item.name,
          },
          {
            id: 'instanceId',
            header: 'Instance ID',
            cell: (item) => item.instance_id,
          },
          {
            id: 'instanceType',
            header: 'Type',
            cell: (item) => item.instance_type,
          },
          {
            id: 'architecture',
            header: 'Architecture',
            cell: (item) =>
              item.cpu_architecture === 'arm64' ? 'ARM64' : 'x86_64',
          },
          {
            id: 'lifecycleState',
            header: 'Lifecycle state',
            cell: (item) => (
              <StatusIndicator
                type={lifecycleIndicatorType(item.lifecycle_state)}
              >
                {item.lifecycle_state}
              </StatusIndicator>
            ),
          },
          {
            id: 'runningJob',
            header: 'Running build job',
            cell: (item) =>
              item.running_build_job_id ? (
                <Link
                  href={`/builds/${item.running_build_job_id}`}
                  onFollow={(event) => {
                    event.preventDefault();
                    navigate(`/builds/${item.running_build_job_id}`);
                  }}
                >
                  {item.running_build_job_id}
                </Link>
              ) : (
                '-'
              ),
          },
          {
            id: 'lastStateChange',
            header: 'Last state change',
            cell: (item) => formatStateChange(item.last_state_change_at),
          },
        ]}
        items={servers}
        loading={loading && !loaded}
        loadingText="Loading build servers"
        variant="container"
        empty={
          <Box textAlign="center" color="inherit">
            <b>No build servers</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              Launch a build server to run dedicated edge component builds.
            </Box>
          </Box>
        }
      />

      {activeModal === 'launch' && (
        <LaunchServerModal
          onSuccess={handleActionSuccess}
          onDismiss={() => setActiveModal(null)}
        />
      )}
      {selectedServer && activeModal === 'terminate' && (
        <TerminateServerModal
          server={selectedServer}
          onSuccess={handleActionSuccess}
          onDismiss={() => setActiveModal(null)}
        />
      )}
    </SpaceBetween>
  );
}
