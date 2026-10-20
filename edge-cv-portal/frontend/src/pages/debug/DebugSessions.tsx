/**
 * Debug sessions page, `/debug` and `/debug/:usecaseId` (workflow-debug-mode
 * design §8, tasks.md 5.2).
 *
 * Per use case: the sessions table (workflow, devices with their shadow
 * result, reported state and counters, state and remaining time, enabledAt,
 * expiresAt, actor, options), "Start debug" for the roles holding
 * `MANAGE_DEVICES`, "Stop" per live session, the enforcement-off banner
 * ("Registry enforcement is off: every signed-in user can read this log",
 * design §7.2 "two switches") and the variant notice for an `x86_64_nvidia`
 * device whose publish failures climb (design §5.6).
 *
 * `/debug` (the navigation entry) resolves the use case like the other
 * use-case-scoped pages: the saved selection or the first use case, then it
 * continues at `/debug/:usecaseId`. The server decides every action; the
 * role predicates only hide controls that would answer 403.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  Select,
  SelectProps,
  SpaceBetween,
} from '@cloudscape-design/components';
import { apiService, isDebugApiError } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { useUsecase } from '../../contexts/UsecaseContext';
import { isRegistryEnforced } from '../../config';
import { getErrorMessage } from '../../utils/errorHandling';
import { canManageDebug } from '../../utils/debugAccess';
import DebugSessionsTable from '../../components/debug/DebugSessionsTable';
import StartDebugModal, { startedNotice } from '../../components/debug/StartDebugModal';
import type { CreateDebugSessionResponse, DebugSession } from './types';

/** The warning of design §7.2 while PORTAL_REGISTRY_ENFORCED is off. */
export const ENFORCEMENT_OFF_BANNER = 'Registry enforcement is off: every signed-in user can read this log';

export default function DebugSessions() {
  const navigate = useNavigate();
  const { usecaseId: routeUsecaseId } = useParams<{ usecaseId: string }>();
  const { user } = useAuth();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const canManage = canManageDebug(user?.role);
  const enforced = isRegistryEnforced();

  const [useCaseOptions, setUseCaseOptions] = useState<SelectProps.Option[]>([]);
  const [sessions, setSessions] = useState<DebugSession[]>([]);
  const [nextPage, setNextPage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});
  const [deviceArchitectures, setDeviceArchitectures] = useState<Record<string, string | null | undefined>>({});

  const usecaseId = routeUsecaseId;

  // Use case options; without a route parameter, continue at the saved or
  // first use case (the "Debug log" navigation entry lands on /debug).
  useEffect(() => {
    let cancelled = false;
    apiService
      .listUseCases()
      .then(({ usecases }) => {
        if (cancelled) return;
        const options = usecases.map((uc) => ({ label: uc.name, value: uc.usecase_id }));
        setUseCaseOptions(options);
        if (!routeUsecaseId) {
          const saved = options.find((o) => o.value === selectedUsecaseId);
          const chosen = saved ?? options[0];
          if (chosen?.value) navigate(`/debug/${encodeURIComponent(chosen.value)}`, { replace: true });
        }
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, 'Failed to list use cases'));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeUsecaseId]);

  useEffect(() => {
    if (usecaseId && usecaseId !== selectedUsecaseId) setSelectedUsecaseId(usecaseId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [usecaseId]);

  const loadSessions = useCallback(() => {
    if (!usecaseId) return;
    setLoading(true);
    setError(null);
    apiService
      .listDebugSessions(usecaseId)
      .then((answer) => {
        setSessions(answer.sessions ?? []);
        setNextPage(answer.next ?? null);
      })
      .catch((err) => setError(getErrorMessage(err, 'Failed to list debug sessions')))
      .finally(() => setLoading(false));
  }, [usecaseId]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // Names and device records are decoration: a failure leaves ids and no
  // variant notice, nothing else.
  useEffect(() => {
    if (!usecaseId) return;
    let cancelled = false;
    apiService
      .listWorkflows(usecaseId)
      .then(({ workflows }) => {
        if (cancelled) return;
        const names: Record<string, string> = {};
        for (const wf of workflows ?? []) names[wf.workflow_id] = wf.name;
        setWorkflowNames(names);
      })
      .catch(() => undefined);
    apiService
      .listDevices(usecaseId)
      .then(({ devices }) => {
        if (cancelled) return;
        const architectures: Record<string, string | null | undefined> = {};
        for (const device of devices ?? []) architectures[device.device_id] = device.target_architecture;
        setDeviceArchitectures(architectures);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [usecaseId]);

  const loadMore = () => {
    if (!usecaseId || !nextPage) return;
    setLoading(true);
    apiService
      .listDebugSessions(usecaseId, { next: nextPage })
      .then((answer) => {
        setSessions((current) => [...current, ...(answer.sessions ?? [])]);
        setNextPage(answer.next ?? null);
      })
      .catch((err) => setError(getErrorMessage(err, 'Failed to list debug sessions')))
      .finally(() => setLoading(false));
  };

  const handleStop = async (session: DebugSession) => {
    try {
      setStoppingId(session.session_id);
      setError(null);
      const result = await apiService.stopDebugSession(session.session_id);
      const failed = result.devices.filter((d) => d.shadow !== 'ok');
      setNotice(
        failed.length
          ? `Session ${result.session_id} stopped; the shadow removal failed on ${failed.map((d) => d.device_id).join(', ')} and is retried by the maintenance run.`
          : `Session ${result.session_id} stopped.`
      );
      loadSessions();
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
    loadSessions();
  };

  const selectedUseCase = useMemo(
    () => useCaseOptions.find((o) => o.value === usecaseId) ?? (usecaseId ? { label: usecaseId, value: usecaseId } : null),
    [useCaseOptions, usecaseId]
  );

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description="Full-fidelity run records of a workflow, sent to the use case's debug log for a limited time. Not part of the dashboards."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => {
                    if (detail.selectedOption.value) {
                      navigate(`/debug/${encodeURIComponent(detail.selectedOption.value)}`);
                    }
                  }}
                  options={useCaseOptions}
                  placeholder="Select a use case"
                  selectedAriaLabel="Selected"
                  data-testid="debug-usecase-select"
                />
                <Button iconName="refresh" onClick={loadSessions} disabled={!usecaseId || loading} ariaLabel="Refresh">
                  Refresh
                </Button>
                {canManage && (
                  <Button
                    variant="primary"
                    onClick={() => setStarting(true)}
                    disabled={!usecaseId}
                    data-testid="start-debug-button"
                  >
                    Start debug
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            Workflow debug sessions
          </Header>
        }
      >
        <SpaceBetween size="m">
          {!enforced && (
            <Alert type="warning" data-testid="enforcement-off-banner">
              {ENFORCEMENT_OFF_BANNER}
            </Alert>
          )}
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          {notice && (
            <Alert type="success" dismissible onDismiss={() => setNotice(null)} data-testid="debug-notice">
              {notice}
            </Alert>
          )}
          {!usecaseId ? (
            <Box textAlign="center" color="text-body-secondary" padding="l">
              Select a use case to list its debug sessions
            </Box>
          ) : (
            <DebugSessionsTable
              sessions={sessions}
              loading={loading}
              usecaseId={usecaseId}
              canManage={canManage}
              onStop={handleStop}
              stoppingId={stoppingId}
              workflowNames={workflowNames}
              deviceArchitectures={deviceArchitectures}
            />
          )}
          {nextPage && (
            <Box textAlign="center">
              <Button onClick={loadMore} loading={loading}>
                Load more
              </Button>
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {starting && usecaseId && (
        <StartDebugModal usecaseId={usecaseId} onDismiss={() => setStarting(false)} onStarted={handleStarted} />
      )}
    </SpaceBetween>
  );
}
