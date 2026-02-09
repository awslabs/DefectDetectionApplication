import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Container,
  SpaceBetween,
  Box,
  Alert,
  BreadcrumbGroup,
  Button,
  Modal,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { Device } from '../types';
import ComponentConfigurationForm from '../components/ComponentConfigurationForm';

export default function ComponentConfiguration() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const componentName = searchParams.get('component_name');
  const usecaseId = searchParams.get('usecase_id');

  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSuccessModal, setShowSuccessModal] = useState(false);
  const [deploymentId, setDeploymentId] = useState<string | null>(null);

  useEffect(() => {
    if (usecaseId) {
      loadDevices();
    }
  }, [usecaseId]);

  const loadDevices = async () => {
    if (!usecaseId) return;

    try {
      setLoading(true);
      setError(null);

      const response = await apiService.listDevices(usecaseId);
      setDevices(response.devices);
    } catch (err) {
      console.error('Failed to load devices:', err);
      setError(err instanceof Error ? err.message : 'Failed to load devices');
    } finally {
      setLoading(false);
    }
  };

  const handleConfigurationSaved = (newDeploymentId: string) => {
    setDeploymentId(newDeploymentId);
    setShowSuccessModal(true);
  };

  const handleViewDeployment = () => {
    if (deploymentId && usecaseId) {
      navigate(`/deployments/${deploymentId}?usecase_id=${usecaseId}`);
    }
  };

  const handleBackToComponent = () => {
    if (componentName && usecaseId) {
      navigate(`/components/${encodeURIComponent(componentName)}?usecase_id=${usecaseId}`);
    }
  };

  if (!componentName || !usecaseId) {
    return (
      <Container>
        <Alert type="error">Missing required parameters: component_name and usecase_id</Alert>
      </Container>
    );
  }

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          Loading devices...
        </Box>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[
          { text: 'Components', href: `/components?usecase_id=${usecaseId}` },
          { text: componentName, href: `/components/${encodeURIComponent(componentName)}?usecase_id=${usecaseId}` },
          { text: 'Configure', href: '#' },
        ]}
        onFollow={(event) => {
          event.preventDefault();
          if (event.detail.href !== '#') {
            navigate(event.detail.href);
          }
        }}
      />

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <ComponentConfigurationForm
        componentName={componentName}
        usecaseId={usecaseId}
        availableDevices={devices.map((device) => ({
          device_id: device.thing_name,
          device_name: device.thing_name,
        }))}
        onConfigurationSaved={handleConfigurationSaved}
        onCancel={handleBackToComponent}
      />

      {/* Success Modal */}
      <Modal
        visible={showSuccessModal}
        onDismiss={() => setShowSuccessModal(false)}
        header="Configuration Deployment Created"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleBackToComponent}>
                Back to Component
              </Button>
              <Button variant="primary" onClick={handleViewDeployment}>
                View Deployment
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            <Box variant="strong">Deployment created successfully!</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">Deployment ID</Box>
            <Box>{deploymentId}</Box>
          </Box>
          <Box>
            You can now view the deployment status and monitor the configuration rollout to your selected devices.
          </Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
