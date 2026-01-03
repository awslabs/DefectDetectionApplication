import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
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

  // Fetch devices for selected use case
  const { data: devicesData } = useQuery({
    queryKey: ['devices', selectedUseCase?.value],
    queryFn: () => apiService.listDevices(selectedUseCase?.value as string),
    enabled: !!selectedUseCase,
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
  const devicesCount = devicesData?.count || 0;
  const onlineDevices = devicesData?.devices?.filter((d: any) => d.status === 'online').length || 0;
  
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

      {/* Use Case Selector */}
      {user?.role === 'PortalAdmin' && (
        <Container>
          <SpaceBetween size="s">
            <Box variant="awsui-key-label">
              Select Use Case
              {user.role === 'PortalAdmin' && (
                <Box variant="small" color="text-status-info" display="inline" margin={{ left: 'xs' }}>
                  (Super User - Access to all use cases)
                </Box>
              )}
            </Box>
            <Select
              selectedOption={selectedUseCase}
              onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
              options={useCaseOptions}
              placeholder="Select a use case to view details"
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
