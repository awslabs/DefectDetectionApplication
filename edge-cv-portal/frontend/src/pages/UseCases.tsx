import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Table,
  Header,
  SpaceBetween,
  Button,
  Modal,
  FormField,
  Input,
  Box,
  Alert,
  ButtonDropdown,
  Flashbar,
  Container,
  StatusIndicator,
  ColumnLayout,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { UseCase } from '../types';
import { useAuth } from '../contexts/AuthContext';
import TeamManagement from '../components/TeamManagement';

interface FormData {
  name: string;
  account_id: string;
  s3_bucket: string;
  s3_prefix?: string;
  cross_account_role_arn: string;
  cost_center?: string;
  // Data Account fields
  data_account_id?: string;
  data_account_role_arn?: string;
  data_account_external_id?: string;
  data_s3_bucket?: string;
  data_s3_prefix?: string;
}

export default function UseCases() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [selectedUseCase, setSelectedUseCase] = useState<UseCase | null>(null);
  const [showTeamModal, setShowTeamModal] = useState(false);
  const [formData, setFormData] = useState<FormData>({
    name: '',
    account_id: '',
    s3_bucket: '',
    s3_prefix: '',
    cross_account_role_arn: '',
    cost_center: '',
  });
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');

  const { data, isLoading } = useQuery({
    queryKey: ['usecases'],
    queryFn: () => apiService.listUseCases(),
  });

  const createMutation = useMutation({
    mutationFn: (data: Partial<UseCase>) => apiService.createUseCase(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['usecases'] });
      setShowCreateModal(false);
      resetForm();
      setSuccessMessage('Use case created successfully');
      setTimeout(() => setSuccessMessage(''), 5000);
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<UseCase> }) =>
      apiService.updateUseCase(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['usecases'] });
      setShowEditModal(false);
      resetForm();
      setSuccessMessage('Use case updated successfully');
      setTimeout(() => setSuccessMessage(''), 5000);
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiService.deleteUseCase(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['usecases'] });
      setShowDeleteModal(false);
      setSelectedUseCase(null);
      setSuccessMessage('Use case deleted successfully');
      setTimeout(() => setSuccessMessage(''), 5000);
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  const provisionMutation = useMutation({
    mutationFn: (usecaseId: string) => apiService.provisionSharedComponents(usecaseId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['usecases'] });
      queryClient.invalidateQueries({ queryKey: ['shared-components-status'] });
      const successCount = data.components.filter(c => c.status === 'shared').length;
      const failedCount = data.components.filter(c => c.status === 'failed').length;
      if (failedCount > 0) {
        const errors = data.components.filter(c => c.error).map(c => `${c.component_name}: ${c.error}`).join('\n');
        setError(`Provisioned ${successCount} component(s), ${failedCount} failed:\n${errors}`);
      } else {
        setSuccessMessage(`Successfully provisioned ${successCount} shared component(s)`);
        setTimeout(() => setSuccessMessage(''), 5000);
      }
    },
    onError: (err: Error) => {
      setError(`Failed to provision shared components: ${err.message}`);
    },
  });

  // Query for shared components status (Portal Admin only)
  const { data: statusData, isLoading: statusLoading } = useQuery({
    queryKey: ['shared-components-status'],
    queryFn: () => apiService.getSharedComponentsStatus(),
    enabled: user?.role === 'PortalAdmin',
  });

  // Mutation for updating all usecases
  const updateAllMutation = useMutation({
    mutationFn: () => apiService.updateAllSharedComponents(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['shared-components-status'] });
      queryClient.invalidateQueries({ queryKey: ['usecases'] });
      if (data.failed_count > 0) {
        setError(`Updated ${data.success_count} usecase(s), ${data.failed_count} failed. Check console for details.`);
        console.log('Update results:', data.results);
      } else {
        setSuccessMessage(`Successfully updated shared components for ${data.success_count} usecase(s) to version ${data.target_version}`);
        setTimeout(() => setSuccessMessage(''), 5000);
      }
    },
    onError: (err: Error) => {
      setError(`Failed to update shared components: ${err.message}`);
    },
  });

  const resetForm = () => {
    setFormData({
      name: '',
      account_id: '',
      s3_bucket: '',
      s3_prefix: '',
      cross_account_role_arn: '',
      cost_center: '',
      data_account_id: '',
      data_account_role_arn: '',
      data_account_external_id: '',
      data_s3_bucket: '',
      data_s3_prefix: '',
    });
    setError('');
    setSelectedUseCase(null);
  };

  const handleCreate = () => {
    if (!formData.name || !formData.account_id || !formData.s3_bucket || !formData.cross_account_role_arn) {
      setError('Name, Account ID, S3 Bucket, and Role ARN are required');
      return;
    }
    createMutation.mutate(formData);
  };

  const handleEdit = (useCase: UseCase) => {
    setSelectedUseCase(useCase);
    setFormData({
      name: useCase.name,
      account_id: useCase.account_id,
      s3_bucket: useCase.s3_bucket,
      s3_prefix: useCase.s3_prefix || '',
      cross_account_role_arn: useCase.cross_account_role_arn,
      cost_center: useCase.cost_center || '',
      data_account_id: useCase.data_account_id || '',
      data_account_role_arn: useCase.data_account_role_arn || '',
      data_account_external_id: useCase.data_account_external_id || '',
      data_s3_bucket: useCase.data_s3_bucket || '',
      data_s3_prefix: useCase.data_s3_prefix || '',
    });
    setShowEditModal(true);
  };

  const handleUpdate = () => {
    if (!selectedUseCase) return;
    if (!formData.name || !formData.account_id || !formData.s3_bucket || !formData.cross_account_role_arn) {
      setError('Name, Account ID, S3 Bucket, and Role ARN are required');
      return;
    }
    updateMutation.mutate({ id: selectedUseCase.usecase_id, data: formData });
  };

  const handleDeleteClick = (useCase: UseCase) => {
    setSelectedUseCase(useCase);
    setShowDeleteModal(true);
  };

  const handleDelete = () => {
    if (!selectedUseCase) return;
    deleteMutation.mutate(selectedUseCase.usecase_id);
  };

  return (
    <SpaceBetween size="l">
      {successMessage && (
        <Flashbar
          items={[
            {
              type: 'success',
              content: successMessage,
              dismissible: true,
              onDismiss: () => setSuccessMessage(''),
            },
          ]}
        />
      )}

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError('')}>
          {error}
        </Alert>
      )}

      {/* Shared Components Status Panel - Portal Admin Only */}
      {user?.role === 'PortalAdmin' && (
        <Container
          header={
            <Header
              variant="h2"
              description="Monitor and update dda-LocalServer components across all usecases"
              actions={
                <Button
                  onClick={() => updateAllMutation.mutate()}
                  loading={updateAllMutation.isPending}
                  disabled={!statusData || statusData.usecases_needing_update === 0}
                >
                  Update All Usecases
                </Button>
              }
            >
              Shared Components Status
            </Header>
          }
        >
          {statusLoading ? (
            <Box textAlign="center" padding="l">Loading status...</Box>
          ) : statusData ? (
            <ColumnLayout columns={4} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Latest Version</Box>
                <Box variant="p">{statusData.latest_version}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Total Usecases</Box>
                <Box variant="p">{statusData.total_usecases}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Needing Update</Box>
                <Box variant="p">
                  {statusData.usecases_needing_update > 0 ? (
                    <StatusIndicator type="warning">
                      {statusData.usecases_needing_update} usecase(s)
                    </StatusIndicator>
                  ) : (
                    <StatusIndicator type="success">All up to date</StatusIndicator>
                  )}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Usecases with Updates Available</Box>
                <Box variant="p">
                  {statusData.usecases
                    .filter(u => u.needs_update)
                    .map(u => u.usecase_name)
                    .join(', ') || 'None'}
                </Box>
              </div>
            </ColumnLayout>
          ) : (
            <Box textAlign="center" color="text-status-inactive">
              Unable to load status
            </Box>
          )}
        </Container>
      )}

      <Table
        header={
          <Header
            variant="h1"
            description="Manage computer vision use cases across AWS accounts"
            actions={
              user ? (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => setShowCreateModal(true)}>
                    Quick Create
                  </Button>
                  <Button 
                    variant="primary" 
                    onClick={() => window.location.href = '/usecases/onboard'}
                  >
                    Onboard New Use Case
                  </Button>
                </SpaceBetween>
              ) : null
            }
          >
            Use Cases
          </Header>
        }
        loading={isLoading}
        items={data?.usecases || []}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: (item: UseCase) => item.name,
            sortingField: 'name',
          },
          {
            id: 'account_id',
            header: 'AWS Account ID',
            cell: (item: UseCase) => item.account_id,
          },
          {
            id: 'owner',
            header: 'Owner',
            cell: (item: UseCase) => item.owner || '-',
          },
          {
            id: 'data_bucket',
            header: 'Data Bucket',
            cell: (item: UseCase) => item.data_s3_bucket || item.s3_bucket,
          },
          {
            id: 'output_bucket',
            header: 'Output Bucket',
            cell: (item: UseCase) => item.s3_bucket,
          },
          {
            id: 'cost_center',
            header: 'Cost Center',
            cell: (item: UseCase) => item.cost_center || '-',
          },
          {
            id: 'created_at',
            header: 'Created',
            cell: (item: UseCase) => new Date(item.created_at).toLocaleDateString(),
            sortingField: 'created_at',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item: UseCase) => (
              <ButtonDropdown
                items={[
                  { id: 'browse', text: 'Browse Datasets' },
                  { id: 'team', text: 'Manage Team' },
                  { id: 'provision', text: 'Re-provision Shared Components' },
                  { id: 'edit', text: 'Edit' },
                  { id: 'delete', text: 'Delete' },
                ]}
                onItemClick={({ detail }) => {
                  if (detail.id === 'browse') {
                    navigate(`/labeling/datasets?usecase_id=${item.usecase_id}`);
                  } else if (detail.id === 'team') {
                    setSelectedUseCase(item);
                    setShowTeamModal(true);
                  } else if (detail.id === 'provision') {
                    provisionMutation.mutate(item.usecase_id);
                  } else if (detail.id === 'edit') {
                    handleEdit(item);
                  } else if (detail.id === 'delete') {
                    handleDeleteClick(item);
                  }
                }}
                expandToViewport
                loading={provisionMutation.isPending}
              >
                Actions
              </ButtonDropdown>
            ),
          },
        ]}
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No use cases</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              Create your first use case to get started.
            </Box>
          </Box>
        }
      />

      {/* Create Modal */}
      <Modal
        visible={showCreateModal}
        onDismiss={() => {
          setShowCreateModal(false);
          resetForm();
        }}
        header="Create Use Case"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowCreateModal(false);
                  resetForm();
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleCreate}
                loading={createMutation.isPending}
              >
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}

          <FormField label="Name" description="A descriptive name for this use case" stretch>
            <Input
              value={formData.name}
              onChange={({ detail }) => setFormData({ ...formData, name: detail.value })}
              placeholder="e.g., Manufacturing Line 1"
            />
          </FormField>

          <FormField
            label="AWS Account ID"
            description="The AWS account where resources are located"
            stretch
          >
            <Input
              value={formData.account_id}
              onChange={({ detail }) => setFormData({ ...formData, account_id: detail.value })}
              placeholder="123456789012"
            />
          </FormField>

          <FormField
            label="S3 Bucket"
            description="S3 bucket for storing datasets and artifacts"
            stretch
          >
            <Input
              value={formData.s3_bucket}
              onChange={({ detail }) => setFormData({ ...formData, s3_bucket: detail.value })}
              placeholder="my-usecase-bucket"
            />
          </FormField>

          <FormField
            label="S3 Prefix (Optional)"
            description="S3 prefix for organizing data within the bucket"
            stretch
          >
            <Input
              value={formData.s3_prefix || ''}
              onChange={({ detail }) => setFormData({ ...formData, s3_prefix: detail.value })}
              placeholder="datasets/"
            />
          </FormField>

          <FormField
            label="Cross-Account Role ARN"
            description="IAM role ARN that the portal will assume"
            stretch
          >
            <Input
              value={formData.cross_account_role_arn}
              onChange={({ detail }) =>
                setFormData({ ...formData, cross_account_role_arn: detail.value })
              }
              placeholder="arn:aws:iam::123456789012:role/PortalAccessRole"
            />
          </FormField>

          <FormField
            label="Cost Center (Optional)"
            description="Cost center for tracking expenses"
            stretch
          >
            <Input
              value={formData.cost_center || ''}
              onChange={({ detail }) => setFormData({ ...formData, cost_center: detail.value })}
              placeholder="CC-12345"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Edit Modal */}
      <Modal
        visible={showEditModal}
        onDismiss={() => {
          setShowEditModal(false);
          resetForm();
        }}
        header="Edit Use Case"
        size="large"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowEditModal(false);
                  resetForm();
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleUpdate}
                loading={updateMutation.isPending}
              >
                Save Changes
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}

          <FormField label="Name" description="A descriptive name for this use case" stretch>
            <Input
              value={formData.name}
              onChange={({ detail }) => setFormData({ ...formData, name: detail.value })}
              placeholder="e.g., Manufacturing Line 1"
            />
          </FormField>

          <FormField
            label="AWS Account ID"
            description="The AWS account where resources are located"
            stretch
          >
            <Input
              value={formData.account_id}
              onChange={({ detail }) => setFormData({ ...formData, account_id: detail.value })}
              placeholder="123456789012"
            />
          </FormField>

          <FormField
            label="S3 Bucket"
            description="S3 bucket for storing datasets and artifacts"
            stretch
          >
            <Input
              value={formData.s3_bucket}
              onChange={({ detail }) => setFormData({ ...formData, s3_bucket: detail.value })}
              placeholder="my-usecase-bucket"
            />
          </FormField>

          <FormField
            label="S3 Prefix (Optional)"
            description="S3 prefix for organizing data within the bucket"
            stretch
          >
            <Input
              value={formData.s3_prefix || ''}
              onChange={({ detail }) => setFormData({ ...formData, s3_prefix: detail.value })}
              placeholder="datasets/"
            />
          </FormField>

          <FormField
            label="Cross-Account Role ARN"
            description="IAM role ARN that the portal will assume"
            stretch
          >
            <Input
              value={formData.cross_account_role_arn}
              onChange={({ detail }) =>
                setFormData({ ...formData, cross_account_role_arn: detail.value })
              }
              placeholder="arn:aws:iam::123456789012:role/PortalAccessRole"
            />
          </FormField>

          <FormField
            label="Cost Center (Optional)"
            description="Cost center for tracking expenses"
            stretch
          >
            <Input
              value={formData.cost_center || ''}
              onChange={({ detail }) => setFormData({ ...formData, cost_center: detail.value })}
              placeholder="CC-12345"
            />
          </FormField>

          <Header variant="h3">Data Account Configuration (Optional)</Header>
          <Alert type="info">
            Configure a separate Data Account if your training data is stored in a different AWS account than your UseCase account.
          </Alert>

          <FormField
            label="Data Account ID"
            description="AWS Account ID where training data is stored"
            stretch
          >
            <Input
              value={formData.data_account_id || ''}
              onChange={({ detail }) => setFormData({ ...formData, data_account_id: detail.value })}
              placeholder="987654321098"
            />
          </FormField>

          <FormField
            label="Data Account Role ARN"
            description="IAM role ARN in the Data Account for accessing S3 buckets"
            stretch
          >
            <Input
              value={formData.data_account_role_arn || ''}
              onChange={({ detail }) => setFormData({ ...formData, data_account_role_arn: detail.value })}
              placeholder="arn:aws:iam::987654321098:role/DDAPortalDataAccessRole"
            />
          </FormField>

          <FormField
            label="Data Account External ID"
            description="External ID for assuming the Data Account role"
            stretch
          >
            <Input
              value={formData.data_account_external_id || ''}
              onChange={({ detail }) => setFormData({ ...formData, data_account_external_id: detail.value })}
              placeholder="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
              type="password"
            />
          </FormField>

          <FormField
            label="Data S3 Bucket (Optional)"
            description="Default S3 bucket in the Data Account"
            stretch
          >
            <Input
              value={formData.data_s3_bucket || ''}
              onChange={({ detail }) => setFormData({ ...formData, data_s3_bucket: detail.value })}
              placeholder="my-data-bucket"
            />
          </FormField>

          <FormField
            label="Data S3 Prefix (Optional)"
            description="S3 prefix within the data bucket"
            stretch
          >
            <Input
              value={formData.data_s3_prefix || ''}
              onChange={({ detail }) => setFormData({ ...formData, data_s3_prefix: detail.value })}
              placeholder="datasets/"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Delete Confirmation Modal */}
      <Modal
        visible={showDeleteModal}
        onDismiss={() => {
          setShowDeleteModal(false);
          setSelectedUseCase(null);
          setError('');
        }}
        header="Delete Use Case"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowDeleteModal(false);
                  setSelectedUseCase(null);
                  setError('');
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleDelete}
                loading={deleteMutation.isPending}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}

          <Alert type="warning">
            This action cannot be undone. Deleting a use case will remove all associated metadata.
          </Alert>

          <Box>
            Are you sure you want to delete the use case{' '}
            <Box variant="strong">{selectedUseCase?.name}</Box>?
          </Box>

          <Box variant="small" color="text-status-inactive">
            Use Case ID: {selectedUseCase?.usecase_id}
          </Box>
        </SpaceBetween>
      </Modal>

      {/* Team Management Modal */}
      {selectedUseCase && (
        <TeamManagement
          visible={showTeamModal}
          onDismiss={() => {
            setShowTeamModal(false);
            setSelectedUseCase(null);
          }}
          usecaseId={selectedUseCase.usecase_id}
          usecaseName={selectedUseCase.name}
        />
      )}
    </SpaceBetween>
  );
}
