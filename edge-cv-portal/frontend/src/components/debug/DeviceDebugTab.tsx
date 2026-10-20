/**
 * The "Debug" tab of the Device detail page (workflow-debug-mode design §8,
 * tasks.md 5.4): the device's debug sessions (every session of the use case
 * that covers this device) with "Start debug" for the roles holding
 * `MANAGE_DEVICES`, the device preselected in the modal. The use case is the
 * device record's. The server decides every action.
 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Header, SpaceBetween } from '@cloudscape-design/components';
import { apiService, isDebugApiError } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { getErrorMessage } from '../../utils/errorHandling';
import { canManageDebug } from '../../utils/debugAccess';
import DebugSessionsTable from './DebugSessionsTable';
import StartDebugModal, { startedNotice } from './StartDebugModal';
import type { CreateDebugSessionResponse, DebugSession } from '../../pages/debug/types';
import type { Device } from '../../types';

/** The sessions of the use case that cover `deviceId`. */
export function sessionsOfDevice(sessions: readonly DebugSession[], deviceId: string): DebugSession[] {
  return sessions.filter((s) => (s.devices ?? []).some((d) => d.device_id === deviceId));
}

export interface DeviceDebugTabProps {
  device: Device;
  usecaseId: string;
}

export default function DeviceDebugTab({ device, usecaseId }: DeviceDebugTabProps) {
  const { user } = useAuth();
  const canManage = canManageDebug(user?.role);

  const [sessions, setSessions] = useState<DebugSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [stoppingId, setStoppingId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    apiService
      .listDebugSessions(usecaseId)
      .then((answer) => setSessions(sessionsOfDevice(answer.sessions ?? [], device.device_id)))
      .catch((err) => setError(getErrorMessage(err, 'Failed to list debug sessions')))
      .finally(() => setLoading(false));
  }, [usecaseId, device.device_id]);

  useEffect(() => {
    load();
  }, [load]);

  const handleStop = async (session: DebugSession) => {
    try {
      setStoppingId(session.session_id);
      setError(null);
      const result = await apiService.stopDebugSession(session.session_id);
      setNotice(`Session ${result.session_id} stopped.`);
      load();
    } catch (err) {
      const remedy = isDebugApiError(err) && err.remedy ? ` ${err.remedy}` : '';
      setError(`${getErrorMessage(err, 'Failed to stop the session')}${remedy}`);
    } finally {
      setStoppingId(null);
    }
  };

  const handleStarted = (result: CreateDebugSessionResponse) => {
    setStarting(false);
    setNotice(startedNotice(result));
    load();
  };

  return (
    <SpaceBetween size="m">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      {notice && (
        <Alert type="success" dismissible onDismiss={() => setNotice(null)} data-testid="device-debug-notice">
          {notice}
        </Alert>
      )}
      <DebugSessionsTable
        sessions={sessions}
        loading={loading}
        usecaseId={usecaseId}
        canManage={canManage}
        onStop={handleStop}
        stoppingId={stoppingId}
        deviceArchitectures={{ [device.device_id]: device.target_architecture }}
        header={
          <Header
            variant="h2"
            counter={`(${sessions.length})`}
            description="Debug sessions covering this device; each device's log opens from its name."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={load} disabled={loading} ariaLabel="Refresh debug sessions">
                  Refresh
                </Button>
                {canManage && (
                  <Button variant="primary" onClick={() => setStarting(true)} data-testid="device-start-debug">
                    Start debug
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            Debug sessions
          </Header>
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>No debug sessions for this device</b>
            <Box variant="p" color="inherit">
              {canManage
                ? 'Start a session to send every run of a workflow on this device to the debug log.'
                : 'An Operator, UseCaseAdmin or PortalAdmin can start one.'}
            </Box>
          </Box>
        }
      />
      {starting && (
        <StartDebugModal
          usecaseId={usecaseId}
          deviceId={device.device_id}
          onDismiss={() => setStarting(false)}
          onStarted={handleStarted}
        />
      )}
    </SpaceBetween>
  );
}
