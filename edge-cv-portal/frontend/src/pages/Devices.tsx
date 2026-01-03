import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
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
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { Device } from '../types';

export default function Devices() {
  const navigate = useNavigate();
  const [filteringText, setFilteringText] = useState('');
  const [selectedItems, setSelectedItems] = useState<Device[]>([]);
  const [showRegisterModal, setShowRegisterModal] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ['devices'],
    queryFn: () => apiService.listDevices(),
  });

  const getStatusIndicator = (status: string) => {
    switch (status) {
      case 'online':
        return <StatusIndicator type="success">Online</StatusIndicator>;
      case 'offline':
        return <StatusIndicator type="stopped">Offline</StatusIndicator>;
      case 'error':
        return <StatusIndicator type="error">Error</StatusIndicator>;
      default:
        return <StatusIndicator type="info">Unknown</StatusIndicator>;
    }
  };

  const formatTimestamp = (timestamp?: number) => {
    if (!timestamp) return 'Never';
    const date = new Date(timestamp);
    const now = Date.now();
    const diff = now - timestamp;

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

  const formatStorage = (used?: number, total?: number) => {
    if (!used || !total) return '-';
    const usedGB = (used / (1024 * 1024 * 1024)).toFixed(1);
    const totalGB = (total / (1024 * 1024 * 1024)).toFixed(1);
    const percentage = ((used / total) * 100).toFixed(0);
    return `${usedGB} / ${totalGB} GB (${percentage}%)`;
  };

  // Filter devices based on search text
  const filteredDevices = (data?.devices || []).filter((device: Device) => {
    if (!filteringText) return true;
    const searchLower = filteringText.toLowerCase();
    return (
      device.device_id.toLowerCase().includes(searchLower) ||
      device.thing_name?.toLowerCase().includes(searchLower) ||
      device.status.toLowerCase().includes(searchLower) ||
      device.usecase_id.toLowerCase().includes(searchLower)
    );
  });

  return (
    <SpaceBetween size="l">
      <Table
        header={
          <Header
            variant="h1"
            description="Monitor and manage edge devices"
            counter={`(${filteredDevices.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  disabled={selectedItems.length === 0}
                  onClick={() => {
                    // TODO: Implement bulk actions
                  }}
                >
                  Restart Selected
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
        loading={isLoading}
        items={filteredDevices}
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        columnDefinitions={[
          {
            id: 'device_id',
            header: 'Device ID',
            cell: (item: Device) => (
              <Link onFollow={() => navigate(`/devices/${item.device_id}`)}>
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
            id: 'usecase_id',
            header: 'Use Case',
            cell: (item: Device) => item.usecase_id,
            sortingField: 'usecase_id',
          },
          {
            id: 'last_heartbeat',
            header: 'Last Seen',
            cell: (item: Device) => formatTimestamp(item.last_heartbeat),
            sortingField: 'last_heartbeat',
          },
          {
            id: 'components',
            header: 'Components',
            cell: (item: Device) => item.components?.length || 0,
          },
          {
            id: 'storage',
            header: 'Storage',
            cell: (item: Device) => formatStorage(item.storage_used, item.storage_total),
          },
          {
            id: 'greengrass_version',
            header: 'Greengrass',
            cell: (item: Device) => item.greengrass_version || '-',
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
              No devices are currently registered.
            </Box>
            <Button onClick={() => setShowRegisterModal(true)}>Register Device</Button>
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
            Device registration happens through AWS IoT Core and AWS IoT Greengrass. Follow the
            steps below to register and provision a new edge device.
          </Alert>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Step 1: Create IoT Thing
            </Box>
            <Box variant="p" padding={{ bottom: 's' }}>
              Create an AWS IoT Thing in the use case account:
            </Box>
            <Box padding="s" color="text-body-secondary">
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '12px' }}>
                {`aws iot create-thing \\
  --thing-name my-edge-device-001 \\
  --attribute-payload '{"usecase_id":"my-usecase-id"}'`}
              </pre>
            </Box>
          </Box>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Step 2: Create and Attach Certificates
            </Box>
            <Box variant="p" padding={{ bottom: 's' }}>
              Generate device certificates and attach to the Thing:
            </Box>
            <Box padding="s" color="text-body-secondary">
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '12px' }}>
                {`aws iot create-keys-and-certificate \\
  --set-as-active \\
  --certificate-pem-outfile device.cert.pem \\
  --public-key-outfile device.public.key \\
  --private-key-outfile device.private.key

aws iot attach-thing-principal \\
  --thing-name my-edge-device-001 \\
  --principal <certificate-arn>`}
              </pre>
            </Box>
          </Box>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Step 3: Install AWS IoT Greengrass
            </Box>
            <Box variant="p" padding={{ bottom: 's' }}>
              Install Greengrass Core on the edge device:
            </Box>
            <Box padding="s" color="text-body-secondary">
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '12px' }}>
                {`# Download Greengrass Core software
curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-nucleus-latest.zip > greengrass-nucleus-latest.zip
unzip greengrass-nucleus-latest.zip -d GreengrassInstaller

# Install with device certificates
sudo -E java -Droot="/greengrass/v2" \\
  -Dlog.store=FILE \\
  -jar ./GreengrassInstaller/lib/Greengrass.jar \\
  --aws-region us-east-1 \\
  --thing-name my-edge-device-001 \\
  --component-default-user ggc_user:ggc_group \\
  --provision false \\
  --deploy-dev-tools true`}
              </pre>
            </Box>
          </Box>

          <Box>
            <Box variant="h3" padding={{ bottom: 's' }}>
              Step 4: Verify Device Registration
            </Box>
            <Box variant="p">
              Once Greengrass is running, the device will automatically appear in the portal's
              device inventory. The portal syncs device information from IoT Core Thing Registry
              and Thing Shadows.
            </Box>
          </Box>

          <Alert type="warning">
            <Box variant="strong">Important:</Box> Ensure the device has the proper IAM policies
            attached to access S3, IoT Core, and Greengrass services in the use case account.
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
