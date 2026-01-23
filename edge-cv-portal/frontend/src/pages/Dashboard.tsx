import { useState, useEffect } from 'react';
import { useQuery, useQueries } from '@tanstack/react-query';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  ColumnLayout,
  StatusIndicator,
  Select,
  SelectProps,
  Cards,
  Link,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useAuth } from '../contexts/AuthContext';

interface RecentEvent {
  id: string;
  type: string;
  message: string;
  timestamp: number;
  status: 'success' | 'error' | 'info' | 'warning';
}

export default function Dashboard() {
  const { user } = useAuth();
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  // Fetch use cases
  const { data: useCasesData, isLoading: useCasesLoading } = useQuery({
    queryKey: ['usecases'],
    queryFn: () => apiService.listUseCases(),
  });

  // Auto-select first use case for non-PortalAdmin users, or when there's only one
  useEffect(() => {
    const usecases = useCasesData?.usecases;
    if (usecases && usecases.length > 0 && !selectedUseCase) {
      // Auto-select if user is not PortalAdmin OR if there's only one use case
      if (user?.role !== 'PortalAdmin' || usecases.length === 1) {
        setSelectedUseCase({
          label: usecases[0].name,
          value: usecases[0].usecase_id,
          description: `Account: ${usecases[0].account_id}`,
        });
      }
    }
  }, [useCasesData, selectedUseCase, user?.role]);

  // Fetch devices for selected use case (single use case mode)
  const { data: devicesData } = useQuery({
    queryKey: ['devices', selectedUseCase?.value],
    queryFn: () => apiService.listDevices(selectedUseCase?.value as string),
    enabled: !!selectedUseCase,
  });

  // Fetch devices for ALL use cases (for aggregate counts when PortalAdmin and no selection)
  const allUseCaseIds = useCasesData?.usecases?.map((uc: any) => uc.usecase_id) || [];
  const deviceQueries = useQueries({
    queries: allUseCaseIds.map((usecaseId: string) => ({
      queryKey: ['devices-aggregate', usecaseId],
      queryFn: () => apiService.listDevices(usecaseId),
      enabled: user?.role === 'PortalAdmin' && !selectedUseCase && allUseCaseIds.length > 0,
      staleTime: 60000, // Cache for 1 minute to avoid too many API calls
    })),
  });

  // Fetch training jobs for selected use case
  const { data: trainingData } = useQuery({
    queryKey: ['training', selectedUseCase?.value],
    queryFn: () => apiService.listTrainingJobs(selectedUseCase?.value as string),
    enabled: !!selectedUseCase,
  });

  // Fetch labeling jobs for selected use case
  const { data: labelingData } = useQuery({
    queryKey: ['labeling', selectedUseCase?.value],
    queryFn: () => apiService.listLabelingJobs({ usecase_id: selectedUseCase?.value as string }),
    enabled: !!selectedUseCase,
  });

  // Transform use cases for selector
  const useCaseOptions: SelectProps.Option[] = useCasesData?.usecases?.map((uc: any) => ({
    label: uc.name,
    value: uc.usecase_id,
    description: `Account: ${uc.account_id}`,
  })) || [];

  // Calculate metrics
  const useCasesCount = useCasesData?.count || 0;
  
  // Calculate device counts - either from selected use case or aggregate
  let devicesCount = 0;
  let onlineDevices = 0;
  
  if (selectedUseCase) {
    // Single use case selected
    devicesCount = devicesData?.count || 0;
    onlineDevices = devicesData?.devices?.filter((d: any) => 
      d.status?.toLowerCase() === 'healthy' || d.status?.toLowerCase() === 'online'
    ).length || 0;
  } else if (user?.role === 'PortalAdmin' && deviceQueries.length > 0) {
    // Aggregate across all use cases for PortalAdmin
    deviceQueries.forEach((query) => {
      if (query.data) {
        devicesCount += query.data.count || 0;
        onlineDevices += query.data.devices?.filter((d: any) => 
          d.status?.toLowerCase() === 'healthy' || d.status?.toLowerCase() === 'online'
        ).length || 0;
      }
    });
  }
  
  // Count active training jobs (InProgress, Pending)
  const activeTrainingJobs = trainingData?.jobs?.filter((job: any) => 
    job.status === 'InProgress' || job.status === 'Pending'
  ).length || 0;
  
  // Count active labeling jobs (InProgress, Pending)
  const activeLabelingJobs = labelingData?.jobs?.filter((job: any) => 
    job.status === 'InProgress' || job.status === 'Pending'
  ).length || 0;
  
  const activeJobs = activeTrainingJobs + activeLabelingJobs;

  // Mock recent events (will be replaced with real data)
  const recentEvents: RecentEvent[] = [
    {
      id: '1',
      type: 'Use Case',
      message: 'Use case created successfully',
      timestamp: Date.now() - 3600000,
      status: 'success',
    },
    {
      id: '2',
      type: 'Device',
      message: 'Device came online',
      timestamp: Date.now() - 7200000,
      status: 'info',
    },
  ];

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'success':
        return <StatusIndicator type="success">Success</StatusIndicator>;
      case 'error':
        return <StatusIndicator type="error">Error</StatusIndicator>;
      case 'warning':
        return <StatusIndicator type="warning">Warning</StatusIndicator>;
      default:
        return <StatusIndicator type="info">Info</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp: number) => {
    const date = new Date(timestamp);
    const now = Date.now();
    const diff = now - timestamp;
    
    if (diff < 3600000) {
      return `${Math.floor(diff / 60000)} minutes ago`;
    } else if (diff < 86400000) {
      return `${Math.floor(diff / 3600000)} hours ago`;
    } else {
      return date.toLocaleDateString();
    }
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={user?.email ? `Welcome back, ${user.email}` : 'Welcome to Edge CV Portal'}
      >
        Dashboard
      </Header>

      {/* Use Case Selector - show for PortalAdmin with multiple use cases */}
      {user?.role === 'PortalAdmin' && useCasesCount > 1 && (
        <Container>
          <SpaceBetween size="s">
            <Box variant="awsui-key-label">
              Select Use Case
              <Box variant="small" color="text-status-info" display="inline" margin={{ left: 'xs' }}>
                (Portal Admin - Access to all use cases)
              </Box>
            </Box>
            <Select
              selectedOption={selectedUseCase}
              onChange={({ detail }) => {
                // If "All Use Cases" selected (empty value), set to null
                if (detail.selectedOption.value === '') {
                  setSelectedUseCase(null);
                } else {
                  setSelectedUseCase(detail.selectedOption);
                }
              }}
              options={[
                { label: 'All Use Cases (Aggregate)', value: '' },
                ...useCaseOptions
              ]}
              placeholder="Select a use case or view aggregate"
              loadingText="Loading use cases..."
              statusType={useCasesLoading ? 'loading' : 'finished'}
              empty="No use cases available"
            />
          </SpaceBetween>
        </Container>
      )}

      {/* Metrics Cards */}
      <ColumnLayout columns={4} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Use Cases</Box>
          <Box variant="h1" fontSize="display-l">
            {useCasesCount}
          </Box>
          <Box variant="small" color="text-status-inactive">
            Total managed use cases
          </Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Total Devices</Box>
          <Box variant="h1" fontSize="display-l">
            {devicesCount}
          </Box>
          <Box variant="small" color="text-status-inactive">
            {selectedUseCase ? `In ${selectedUseCase.label}` : 'Across all use cases'}
          </Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Online Devices</Box>
          <Box variant="h1" fontSize="display-l">
            <StatusIndicator type={onlineDevices > 0 ? 'success' : 'stopped'}>
              {onlineDevices}
            </StatusIndicator>
          </Box>
          <Box variant="small" color="text-status-inactive">
            {devicesCount > 0 ? `${Math.round((onlineDevices / devicesCount) * 100)}% online` : 'No devices'}
          </Box>
        </Container>

        <Container>
          <Box variant="awsui-key-label">Active Jobs</Box>
          <Box variant="h1" fontSize="display-l">
            {activeJobs}
          </Box>
          <Box variant="small" color="text-status-inactive">
            Training & labeling jobs
          </Box>
        </Container>
      </ColumnLayout>

      {/* Recent Events Timeline */}
      <Container header={<Header variant="h2">Recent Events</Header>}>
        {recentEvents.length > 0 ? (
          <Cards
            cardDefinition={{
              header: (item) => (
                <Box>
                  <Box variant="strong">{item.type}</Box>
                  <Box variant="small" color="text-status-inactive">
                    {formatTimestamp(item.timestamp)}
                  </Box>
                </Box>
              ),
              sections: [
                {
                  id: 'status',
                  content: (item) => getStatusIcon(item.status),
                },
                {
                  id: 'message',
                  content: (item) => item.message,
                },
              ],
            }}
            items={recentEvents}
            cardsPerRow={[{ cards: 1 }]}
          />
        ) : (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            No recent events
          </Box>
        )}
      </Container>

      {/* Quick Links */}
      <Container header={<Header variant="h2">Quick Actions</Header>}>
        <ColumnLayout columns={3}>
          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Use Cases
            </Box>
            <SpaceBetween size="xs">
              <Link href="/usecases">View all use cases</Link>
              <Link href="/usecases/create">Create new use case</Link>
            </SpaceBetween>
          </Box>
          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Devices
            </Box>
            <SpaceBetween size="xs">
              <Link href="/devices">View device inventory</Link>
              <Link href="/devices/health">Check device health</Link>
            </SpaceBetween>
          </Box>
          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Jobs
            </Box>
            <SpaceBetween size="xs">
              <Link>Start training job</Link>
              <Link>Create labeling job</Link>
            </SpaceBetween>
          </Box>
        </ColumnLayout>
      </Container>
    </SpaceBetween>
  );
}
