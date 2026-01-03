import { useState, useEffect } from 'react';
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
} from '@cloudscape-design/components';

interface AuditLogEntry {
  event_id: string;
  timestamp: number;
  user_id: string;
  usecase_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  result: 'success' | 'failure';
  details: Record<string, any>;
  ip_address: string;
  is_super_user: boolean;
}

export default function AuditLogs() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [dateRange, setDateRange] = useState<DateRangePickerProps.Value | null>(null);
  const [selectedAction, setSelectedAction] = useState<SelectProps.Option | null>(null);
  const [selectedUser, setSelectedUser] = useState<SelectProps.Option | null>(null);
  const [searchText, setSearchText] = useState('');

  useEffect(() => {
    loadLogs();
  }, [dateRange, selectedAction, selectedUser, searchText]);

  const loadLogs = async () => {
    setLoading(true);
    try {
      // Mock data for now
      const mockLogs: AuditLogEntry[] = [
        {
          event_id: 'evt-001',
          timestamp: Date.now() - 3600000,
          user_id: 'user@example.com',
          usecase_id: 'usecase-001',
          action: 'create_training_job',
          resource_type: 'training_job',
          resource_id: 'training-001',
          result: 'success',
          details: { model_name: 'DefectDetectionModel', instance_type: 'ml.p3.2xlarge' },
          ip_address: '192.168.1.100',
          is_super_user: false,
        },
        {
          event_id: 'evt-002',
          timestamp: Date.now() - 7200000,
          user_id: 'admin@example.com',
          usecase_id: 'usecase-001',
          action: 'create_deployment',
          resource_type: 'deployment',
          resource_id: 'deploy-001',
          result: 'success',
          details: { component_arn: 'arn:aws:greengrass:...', target_devices: 3 },
          ip_address: '192.168.1.101',
          is_super_user: true,
        },
        {
          event_id: 'evt-003',
          timestamp: Date.now() - 10800000,
          user_id: 'user@example.com',
          usecase_id: 'usecase-001',
          action: 'delete_model',
          resource_type: 'model',
          resource_id: 'model-005',
          result: 'failure',
          details: { error: 'Model is currently deployed' },
          ip_address: '192.168.1.100',
          is_super_user: false,
        },
        {
          event_id: 'evt-004',
          timestamp: Date.now() - 14400000,
          user_id: 'admin@example.com',
          usecase_id: 'usecase-002',
          action: 'create_usecase',
          resource_type: 'usecase',
          resource_id: 'usecase-002',
          result: 'success',
          details: { name: 'Production Line 2', account_id: '123456789012' },
          ip_address: '192.168.1.101',
          is_super_user: true,
        },
        {
          event_id: 'evt-005',
          timestamp: Date.now() - 18000000,
          user_id: 'user@example.com',
          usecase_id: 'usecase-001',
          action: 'create_labeling_job',
          resource_type: 'labeling_job',
          resource_id: 'label-001',
          result: 'success',
          details: { task_type: 'ObjectDetection', images_count: 1000 },
          ip_address: '192.168.1.100',
          is_super_user: false,
        },
      ];
      setLogs(mockLogs);
    } catch (error) {
      console.error('Failed to load audit logs:', error);
    } finally {
      setLoading(false);
    }
  };

  const actionOptions: SelectProps.Option[] = [
    { label: 'All Actions', value: '' },
    { label: 'Create Training Job', value: 'create_training_job' },
    { label: 'Create Deployment', value: 'create_deployment' },
    { label: 'Create Use Case', value: 'create_usecase' },
    { label: 'Delete Model', value: 'delete_model' },
    { label: 'Create Labeling Job', value: 'create_labeling_job' },
  ];

  const userOptions: SelectProps.Option[] = [
    { label: 'All Users', value: '' },
    { label: 'user@example.com', value: 'user@example.com' },
    { label: 'admin@example.com', value: 'admin@example.com' },
  ];

  const getResultBadge = (result: 'success' | 'failure') => {
    return result === 'success' ? (
      <Badge color="green">Success</Badge>
    ) : (
      <Badge color="red">Failure</Badge>
    );
  };

  const formatAction = (action: string) => {
    return action
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  };

  return (
    <Container
      header={
        <Header variant="h1" description="View and filter audit logs for all portal actions">
          Audit Logs
        </Header>
      }
    >
      <SpaceBetween size="l">
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
                formatRelativeRange: (e) => {
                  const unit = e.unit === 'hour' ? 'hour' : 'day';
                  return `Last ${e.amount} ${unit}${e.amount > 1 ? 's' : ''}`;
                },
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

          <FormField label="Action">
            <Select
              selectedOption={selectedAction}
              onChange={({ detail }) => setSelectedAction(detail.selectedOption)}
              options={actionOptions}
              placeholder="All actions"
            />
          </FormField>

          <FormField label="User">
            <Select
              selectedOption={selectedUser}
              onChange={({ detail }) => setSelectedUser(detail.selectedOption)}
              options={userOptions}
              placeholder="All users"
            />
          </FormField>

          <FormField label="Search">
            <Input
              value={searchText}
              onChange={({ detail }) => setSearchText(detail.value)}
              placeholder="Search resource ID..."
            />
          </FormField>
        </SpaceBetween>

        <Table
          columnDefinitions={[
            {
              id: 'timestamp',
              header: 'Timestamp',
              cell: item => new Date(item.timestamp).toLocaleString(),
              sortingField: 'timestamp',
            },
            {
              id: 'user',
              header: 'User',
              cell: item => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Box>{item.user_id}</Box>
                  {item.is_super_user && (
                    <Badge color="blue">Super User</Badge>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'action',
              header: 'Action',
              cell: item => formatAction(item.action),
            },
            {
              id: 'resource',
              header: 'Resource',
              cell: item => (
                <Box>
                  <Box fontWeight="bold">{item.resource_type}</Box>
                  <Box fontSize="body-s" color="text-body-secondary">
                    {item.resource_id}
                  </Box>
                </Box>
              ),
            },
            {
              id: 'result',
              header: 'Result',
              cell: item => getResultBadge(item.result),
            },
            {
              id: 'ip',
              header: 'IP Address',
              cell: item => item.ip_address,
            },
          ]}
          items={logs}
          loading={loading}
          loadingText="Loading audit logs"
          sortingDisabled={false}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No audit logs</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                No audit logs found matching the current filters.
              </Box>
            </Box>
          }
        />
      </SpaceBetween>
    </Container>
  );
}
