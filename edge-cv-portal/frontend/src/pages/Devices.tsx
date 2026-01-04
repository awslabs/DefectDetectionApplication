import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Table,
  Header,
  SpaceBetween,
  StatusIndicator,
  Box,
  TextFilter,
  Button,
  Link,
  Modal,
  Alert,
  Select,
  SelectProps,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { Device, UseCase } from '../types';

export default function Devices() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [filteringText, setFilteringText] = useState('');
  const [selectedItems, setSelectedItems] = useState<Device[]>([]);
  const [showRegisterModal, setShowRegisterModal] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  // Use case management
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  // Load use cases on mount
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        
        // Check for URL parameter first
        const urlUseCaseId = searchParams.get('usecase_id');
        if (urlUseCaseId) {
          const preSelectedUseCase = useCaseList.find((uc: UseCase) => uc.usecase_id === urlUseCaseId);
          if (preSelectedUseCase) {
            setSelectedUseCase({
              label: preSelectedUseCase.name,
              value: preSelectedUseCase.usecase_id,
            });
            return;
          }
        }
        
        // Auto-select first use case if available
        if (useCaseList.length > 0) {
          setSelectedUseCase({
            label: useCaseList[0].name,
            value: useCaseList[0].usecase_id,
          });
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
      }
    };
    loadUseCases();
  }, [searchParams]);

  // Load devices when use case changes
  useEffect(() => {
    if (selectedUseCase?.value) {
      loadDevices();
    } else {
      setDevices([]);
      setLoading(false);
    }
  }, [selectedUseCase]);

  const loadDevices = async () => {
    if (!selectedUseCase?.value) return;
    
    try {
      setLoading(true);
      setError(null);
      const response = await apiService.listDevices(selectedUseCase.value);
      setDevices(response.devices || []);
    } catch (err: any) {
      console.error('Failed to load devices:', err);
      setError(err.message || 'Failed to load devices');
      setDevices([]);
    } finally {
      setLoading(false);
    }
  };

  const getStatusIndicator = (status: string) => {
    const statusLower = status?.toLowerCase() || 'unknown';
    switch (statusLower) {
      case 'healthy':
      case 'online':
        return <StatusIndicator type="success">Healthy</StatusIndicator>;
      case 'unhealthy':
      case 'offline':
        return <StatusIndicator type="error">Unhealthy</StatusIndicator>;
      case 'error':
        return <StatusIndicator type="error">Error</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status || 'Unknown'}</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp?: string | number) => {
    if (!timestamp) return 'Never';
    const date = new Date(timestamp);
    const now = Date.now();
    const diff = now - date.getTime();

    if (diff < 60000) {
      return 'Just now';
    } else if (diff < 3600000) {
      return `${Math.floor(diff / 60000)} minutes ago`;
    } else if (diff < 86400000) {
      return `${Math.floor(diff / 3600000)} hours ago`;
    } else {
      return date.toLocaleString();
    }
  };

  // Filter devices based on search text
  const filteredDevices = devices.filter((device: Device) => {
    if (!filteringText) return true;
    const searchLower = filteringText.toLowerCase();
    return (
      device.device_id.toLowerCase().includes(searchLower) ||
      device.thing_name?.toLowerCase().includes(searchLower) ||
      device.status?.toLowerCase().includes(searchLower) ||
      device.greengrass_version?.toLowerCase().includes(searchLower) ||
      device.platform?.toLowerCase().includes(searchLower)
    );
  });

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      
      <Table
        header={
          <Header
            variant="h1"
            description="Monitor and manage Greengrass core devices set up via setup_station.sh"
            counter={`(${filteredDevices.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Box variant="span">Use Case:</Box>
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => setSelectedUseCase(detail.selectedOption)}
                  placeholder="Select use case"
                  options={useCases.map((uc) => ({
                    label: uc.name,
                    value: uc.usecase_id,
                  }))}
                />
                <Button
                  iconName="refresh"
                  onClick={loadDevices}
                  loading={loading}
                  disabled={!selectedUseCase}
                >
                  Refresh
                </Button>
                <Button variant="primary" onClick={() => setShowRegisterModal(true)}>
                  Register Device
                </Button>
              </SpaceBetween>
            }
          >
            Devices
          </Header>
        }
        loading={loading}
        items={filteredDevices}
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        columnDefinitions={[
          {
            id: 'device_id',
            header: 'Device ID',
            cell: (item: Device) => (
              <Link onFollow={() => navigate(`/devices/${item.device_id}?usecase_id=${selectedUseCase?.value}`)}>
                {item.device_id}
              </Link>
            ),
            sortingField: 'device_id',
          },
          {
            id: 'thing_name',
            header: 'Thing Name',
            cell: (item: Device) => item.thing_name || '-',
            sortingField: 'thing_name',
          },
          {
            id: 'status',
            header: 'Status',
            cell: (item: Device) => getStatusIndicator(item.status),
            sortingField: 'status',
          },
          {
            id: 'greengrass_version',
            header: 'Greengrass Version',
            cell: (item: Device) => item.greengrass_version || '-',
          },
          {
            id: 'platform',
            header: 'Platform',
            cell: (item: Device) => item.platform || '-',
          },
          {
            id: 'architecture',
            header: 'Architecture',
            cell: (item: Device) => item.architecture || '-',
          },
          {
            id: 'last_status_update',
            header: 'Last Seen',
            cell: (item: Device) => formatTimestamp(item.last_status_update),
            sortingField: 'last_status_update',
          },
          {
            id: 'components',
            header: 'Components',
            cell: (item: Device) => item.installed_components?.length || 0,
          },
        ]}
        filter={
          <TextFilter
            filteringText={filteringText}
            filteringPlaceholder="Search devices"
            filteringAriaLabel="Filter devices"
            onChange={({ detail }) => setFilteringText(detail.filteringText)}
          />
        }
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No devices</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {selectedUseCase 
                ? 'No portal-managed devices found. Devices must be set up using setup_station.sh to appear here.'
                : 'Select a use case to view devices.'}
            </Box>
            {selectedUseCase && (
              <Button onClick={() => setShowRegisterModal(true)}>Register Device</Button>
            )}
          </Box>
        }
        variant="full-page"
      />

      {/* Register Device Modal */}
      <Modal
        visible={showRegisterModal}
        onDismiss={() => setShowRegisterModal(false)}
        header="Register Edge Device"
        size="large"
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setShowRegisterModal(false)}>
              Close
            </Button>
          </Box>
        }
      >
        <SpaceBetween size="l">
          <Alert type="info">
            Devices are registered using the <code>setup_station.sh</code> script which provisions 
            the device with AWS IoT Greengrass and tags it for portal discovery.
          </Alert>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Prerequisites
            </Box>
            <Box variant="p">
              Before running the setup script, ensure:
            </Box>
            <ul>
              <li>AWS CLI is configured with appropriate credentials</li>
              <li>The device has Ubuntu 18.04+ or compatible Linux distribution</li>
              <li>Root/sudo access is available on the device</li>
            </ul>
          </Box>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Run Setup Script
            </Box>
            <Box variant="p" padding={{ bottom: 's' }}>
              Copy the setup script to your device and run:
            </Box>
            <Box padding="s" color="text-body-secondary">
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '12px' }}>
                {`# Copy the station_install folder to your device
scp -r station_install/ user@device:/tmp/

# SSH to the device and run setup
ssh user@device
cd /tmp/station_install
sudo ./setup_station.sh us-east-1 my-device-name`}
              </pre>
            </Box>
          </Box>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              What the Script Does
            </Box>
            <ul>
              <li>Installs Python 3.9, Java, Docker, and other dependencies</li>
              <li>Downloads and installs AWS IoT Greengrass Core v2</li>
              <li>Creates an IoT Thing and provisions certificates</li>
              <li>Tags the IoT Thing with <code>dda-portal:managed=true</code> for portal discovery</li>
              <li>Sets up required users, groups, and permissions</li>
            </ul>
          </Box>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              After Setup
            </Box>
            <Box variant="p">
              Once the script completes successfully, the device will automatically appear in this 
              portal within a few minutes. The device status will show as "Healthy" once Greengrass 
              is running and connected.
            </Box>
          </Box>

          <Alert type="warning">
            <Box variant="strong">For Existing Devices:</Box> If you have devices that were set up 
            before the portal tagging feature, you can manually tag them:
            <Box padding="s" color="text-body-secondary">
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '12px' }}>
{`aws iot tag-resource \\
  --resource-arn arn:aws:iot:REGION:ACCOUNT:thing/THING_NAME \\
  --tags "Key=dda-portal:managed,Value=true"`}
              </pre>
            </Box>
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
