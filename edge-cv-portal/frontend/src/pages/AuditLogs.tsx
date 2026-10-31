import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Container,
  Header,
  Table,
  SpaceBetween,
  Box,
  Badge,
  DateRangePicker,
  DateRangePickerProps,
  FormField,
  Select,
  SelectProps,
  Input,
  Button,
  Alert,
} from '@cloudscape-design/components';
import { useAuth } from '../contexts/AuthContext';
import { useUsecase } from '../contexts/UsecaseContext';
import apiService from '../services/api';
import { useTableSort } from '../hooks/useTableSort';

interface AuditLogEntry {
  event_id: string;
  timestamp: number;
  user_id: string;
  usecase_id?: string;
  action: string;
  resource_type: string;
  resource_id: string;
  result: string;
  details?: Record<string, any>;
  channel?: string;
  mcp_tool?: string;
}

// Channel filter values accepted by GET /audit-logs. Entries written before
// the channel attribute existed are returned under 'api' (Web & API).
const CHANNEL_OPTIONS: SelectProps.Option[] = [
  { label: 'All channels', value: '' },
  { label: 'Web & API', value: 'api' },
  { label: 'MCP', value: 'mcp' },
];

interface UseCase {
  usecase_id: string;
  name: string;
}

export default function AuditLogs() {
  const { user } = useAuth();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dateRange, setDateRange] = useState<DateRangePickerProps.Value | null>(null);
  const [selectedAction, setSelectedAction] = useState<SelectProps.Option | null>(null);
  const [selectedUsecase, setSelectedUsecase] = useState<SelectProps.Option | null>(null);
  const [selectedChannel, setSelectedChannel] = useState<SelectProps.Option>(CHANNEL_OPTIONS[0]);
  const [searchText, setSearchText] = useState('');
  const [availableActions, setAvailableActions] = useState<string[]>([]);
  const [usecases, setUsecases] = useState<UseCase[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [nextToken, setNextToken] = useState<string | undefined>();
  const [totalCount, setTotalCount] = useState(0);

  // Sequence number of the latest getAuditLogs request. Each request is a
  // separate scan and responses can arrive out of order, so only the latest
  // request's response is applied.
  const requestSeq = useRef(0);

  const isPortalAdmin = user?.role === 'PortalAdmin';

  // Load usecases for filter dropdown
  useEffect(() => {
    // An older response must not re-apply a saved use case the user has
    // since changed.
    let cancelled = false;
    const loadUsecases = async () => {
      try {
        const response = await apiService.listUseCases();
        if (cancelled) return;
        const usecaseList = response.usecases || [];
        setUsecases(usecaseList);
        
        // Use saved selection from context if available
        if (selectedUsecaseId) {
          const saved = usecaseList.find(uc => uc.usecase_id === selectedUsecaseId);
          if (saved) {
            setSelectedUsecase({
              label: saved.name,
              value: saved.usecase_id,
            });
          }
        }
      } catch (err) {
        console.error('Failed to load usecases:', err);
      }
    };
    loadUsecases();
    return () => {
      cancelled = true;
    };
  }, [selectedUsecaseId]);

  // The loader depends on the filter values, not the option objects, so
  // re-selecting the same use case (e.g. when listUseCases resolves) does not
  // trigger another fetch.
  const usecaseFilter = selectedUsecase?.value || undefined;
  const channelFilter = selectedChannel.value;
  const actionFilter = selectedAction?.value || undefined;

  // Loads the first page for the current filters, or, given a pagination
  // token, appends the next page. Every request carries the current filters.
  const loadLogs = useCallback(async (pageToken?: string) => {
    const seq = ++requestSeq.current;
    const append = !!pageToken;
    setLoading(true);
    setError(null);
    if (!append) {
      // The previous token belongs to the previous filters' scan.
      setNextToken(undefined);
    }

    try {
      const params: {
        usecase_id?: string;
        action?: string;
        start_time?: number;
        end_time?: number;
        limit?: number;
        next_token?: string;
        channel?: 'api' | 'mcp';
      } = { limit: 50 };

      if (usecaseFilter) {
        params.usecase_id = usecaseFilter;
      }

      if (channelFilter === 'api' || channelFilter === 'mcp') {
        params.channel = channelFilter;
      }

      if (actionFilter) {
        params.action = actionFilter;
      }

      if (dateRange) {
        if (dateRange.type === 'relative') {
          const now = Date.now();
          const amount = dateRange.amount || 1;
          const unit = dateRange.unit || 'day';
          let msOffset = 0;
          
          switch (unit) {
            case 'hour': msOffset = amount * 60 * 60 * 1000; break;
            case 'day': msOffset = amount * 24 * 60 * 60 * 1000; break;
            case 'week': msOffset = amount * 7 * 24 * 60 * 60 * 1000; break;
            case 'month': msOffset = amount * 30 * 24 * 60 * 60 * 1000; break;
          }
          
          params.start_time = now - msOffset;
          params.end_time = now;
        } else if (dateRange.type === 'absolute' && dateRange.startDate && dateRange.endDate) {
          params.start_time = new Date(dateRange.startDate).getTime();
          params.end_time = new Date(dateRange.endDate).getTime();
        }
      }

      if (pageToken) {
        params.next_token = pageToken;
      }

      const response = await apiService.getAuditLogs(params);
      if (seq !== requestSeq.current) return;

      const page = response.logs || [];
      setLogs(prev => (append ? [...prev, ...page] : page));
      setTotalCount(prev => (append ? prev : 0) + (response.count || 0));
      setAvailableActions(response.available_actions || []);
      setIsAdmin(response.is_admin || false);
      setNextToken(response.next_token);
    } catch (err: any) {
      if (seq !== requestSeq.current) return;
      console.error('Failed to load audit logs:', err);
      setError(err.message || 'Failed to load audit logs');
      setLogs([]);
    } finally {
      if (seq === requestSeq.current) {
        setLoading(false);
      }
    }
  }, [dateRange, actionFilter, usecaseFilter, channelFilter]);

  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  const actionOptions: SelectProps.Option[] = [
    { label: 'All Actions', value: '' },
    ...availableActions.map(action => ({
      label: formatAction(action),
      value: action,
    })),
  ];

  const usecaseOptions: SelectProps.Option[] = [
    { label: 'All Use Cases', value: '' },
    ...usecases.map(uc => ({
      label: uc.name,
      value: uc.usecase_id,
    })),
  ];

  const getResultBadge = (result: string) => {
    switch (result) {
      case 'success': return <Badge color="green">Success</Badge>;
      case 'failure':
      case 'failed': return <Badge color="red">Failure</Badge>;
      case 'denied': return <Badge color="red">Denied</Badge>;
      default: return <Badge>{result}</Badge>;
    }
  };

  const filteredLogs = searchText
    ? logs.filter(log => 
        log.resource_id?.toLowerCase().includes(searchText.toLowerCase()) ||
        log.user_id?.toLowerCase().includes(searchText.toLowerCase())
      )
    : logs;

  const { items: sortedLogs, sortingProps } = useTableSort(filteredLogs);

  if (!isPortalAdmin && user?.role !== 'UseCaseAdmin') {
    return (
      <Container header={<Header variant="h1">Audit Logs</Header>}>
        <Alert type="warning">
          You don't have permission to view audit logs. This feature is available to Use Case Admins and Portal Admins.
        </Alert>
      </Container>
    );
  }

  return (
    <Container
      header={
        <Header 
          variant="h1" 
          description={isAdmin 
            ? "View audit logs for all portal actions across all use cases" 
            : "View audit logs for your assigned use cases"
          }
          actions={<Button onClick={() => loadLogs()} iconName="refresh">Refresh</Button>}
        >
          Audit Logs
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <SpaceBetween direction="horizontal" size="m">
          <FormField label="Date Range">
            <DateRangePicker
              value={dateRange}
              onChange={({ detail }) => setDateRange(detail.value)}
              placeholder="Filter by date range"
              relativeOptions={[
                { key: 'previous-1-hour', amount: 1, unit: 'hour', type: 'relative' },
                { key: 'previous-6-hours', amount: 6, unit: 'hour', type: 'relative' },
                { key: 'previous-1-day', amount: 1, unit: 'day', type: 'relative' },
                { key: 'previous-7-days', amount: 7, unit: 'day', type: 'relative' },
                { key: 'previous-30-days', amount: 30, unit: 'day', type: 'relative' },
              ]}
              isValidRange={() => ({ valid: true })}
              i18nStrings={{
                todayAriaLabel: 'Today',
                nextMonthAriaLabel: 'Next month',
                previousMonthAriaLabel: 'Previous month',
                customRelativeRangeDurationLabel: 'Duration',
                customRelativeRangeDurationPlaceholder: 'Enter duration',
                customRelativeRangeOptionLabel: 'Custom range',
                customRelativeRangeOptionDescription: 'Set a custom range in the past',
                customRelativeRangeUnitLabel: 'Unit of time',
                formatRelativeRange: (e) => `Last ${e.amount} ${e.unit}${e.amount > 1 ? 's' : ''}`,
                formatUnit: (unit, value) => (value === 1 ? unit : `${unit}s`),
                dateTimeConstraintText: 'Range must be between 6 and 30 days.',
                relativeModeTitle: 'Relative range',
                absoluteModeTitle: 'Absolute range',
                relativeRangeSelectionHeading: 'Choose a range',
                startDateLabel: 'Start date',
                endDateLabel: 'End date',
                startTimeLabel: 'Start time',
                endTimeLabel: 'End time',
                clearButtonLabel: 'Clear',
                cancelButtonLabel: 'Cancel',
                applyButtonLabel: 'Apply',
              }}
            />
          </FormField>

          <FormField label="Use Case">
            <Select
              selectedOption={selectedUsecase}
              onChange={({ detail }) => {
                setSelectedUsecase(detail.selectedOption);
                setSelectedUsecaseId(detail.selectedOption?.value || null);
              }}
              options={usecaseOptions}
              placeholder="All use cases"
            />
          </FormField>

          <FormField label="Channel">
            <Select
              selectedOption={selectedChannel}
              onChange={({ detail }) => setSelectedChannel(detail.selectedOption)}
              options={CHANNEL_OPTIONS}
              ariaLabel="Channel"
            />
          </FormField>

          <FormField label="Action">
            <Select
              selectedOption={selectedAction}
              onChange={({ detail }) => setSelectedAction(detail.selectedOption)}
              options={actionOptions}
              placeholder="All actions"
            />
          </FormField>

          <FormField label="Search">
            <Input
              value={searchText}
              onChange={({ detail }) => setSearchText(detail.value)}
              placeholder="Search user or resource..."
            />
          </FormField>
        </SpaceBetween>

        <Table
          resizableColumns
          columnDefinitions={[
            {
              id: 'timestamp',
              header: 'Timestamp',
              cell: item => new Date(item.timestamp).toLocaleString(),
              sortingField: 'timestamp',
              width: 180,
            },
            {
              id: 'user',
              header: 'User',
              cell: item => item.user_id,
              width: 200,
            },
            {
              id: 'usecase',
              header: 'Use Case',
              cell: item => {
                if (!item.usecase_id || item.usecase_id === 'global') {
                  return <Box color="text-status-inactive">Global</Box>;
                }
                const usecase = usecases.find(uc => uc.usecase_id === item.usecase_id);
                return usecase?.name || item.usecase_id;
              },
              width: 150,
            },
            {
              id: 'action',
              header: 'Action',
              cell: item => formatAction(item.action),
              width: 180,
            },
            {
              id: 'resource',
              header: 'Resource',
              cell: item => (
                <Box>
                  <Box fontWeight="bold">{item.resource_type}</Box>
                  <Box fontSize="body-s" color="text-body-secondary">{item.resource_id}</Box>
                </Box>
              ),
              width: 200,
            },
            {
              id: 'result',
              header: 'Result',
              cell: item => getResultBadge(item.result),
              width: 100,
            },
            {
              id: 'details',
              header: 'Details',
              cell: item => {
                const hasDetails = !!item.details && Object.keys(item.details).length > 0;
                if (!item.mcp_tool && !hasDetails) return '-';
                if (!hasDetails) {
                  return <Box fontSize="body-s">mcp_tool: {item.mcp_tool}</Box>;
                }
                const keys = Object.keys(item.details!).slice(0, 2);
                return (
                  <Box fontSize="body-s">
                    {item.mcp_tool && <div>mcp_tool: {item.mcp_tool}</div>}
                    {keys.map(key => (
                      <div key={key}>
                        {key}: {String(item.details![key]).substring(0, 30)}
                        {String(item.details![key]).length > 30 ? '...' : ''}
                      </div>
                    ))}
                    {Object.keys(item.details!).length > 2 && (
                      <Box color="text-status-inactive">+{Object.keys(item.details!).length - 2} more</Box>
                    )}
                  </Box>
                );
              },
            },
          ]}
          items={sortedLogs}
          {...sortingProps}
          loading={loading}
          loadingText="Loading audit logs"
          sortingDisabled={false}
          variant="container"
          stickyHeader
          empty={
            <Box textAlign="center" color="inherit">
              <b>No audit logs</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                {error ? 'Failed to load audit logs. Please try again.' : 'No audit logs found matching the current filters.'}
              </Box>
            </Box>
          }
          footer={
            nextToken && (
              <Box textAlign="center" padding="s">
                <Button onClick={() => loadLogs(nextToken)}>Load More</Button>
              </Box>
            )
          }
        />

        {totalCount > 0 && (
          <Box textAlign="center" color="text-body-secondary">
            Showing {filteredLogs.length} of {totalCount} logs
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}

function formatAction(action: string): string {
  return action.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}
