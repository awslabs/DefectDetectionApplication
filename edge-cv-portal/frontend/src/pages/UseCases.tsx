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
  Toggle,
} from '@cloudscape-design/components';
import {
  ApiError,
  apiService,
  RegistrationReport,
  UsecaseBucketUpdateResponse,
} from '../services/api';
import { UseCase } from '../types';
import { useAuth } from '../contexts/AuthContext';
import TeamManagement from '../components/TeamManagement';
import BucketNotAllowedNotice, {
  USECASE_ADMIN_REQUIRED_MESSAGE,
} from '../components/BucketNotAllowedNotice';
import { validateBucketName } from '../utils/s3Validation';
import { bucketErrorCode } from '../utils/bucketErrors';
import { useTableSort } from '../hooks/useTableSort';
import { useUsecasePermissions } from '../hooks/useUsecasePermissions';
import {
  BucketRemedy,
  RegistrationRemedies,
  registrationRemedies,
} from '../components/RegistrationRemedy';

interface FormDataType {
  name: string;
  account_id: string;
  s3_bucket: string;
  s3_prefix: string;
  cross_account_role_arn: string;
  data_account_id: string;
  data_account_role_arn: string;
  data_account_external_id: string;
  data_s3_bucket: string;
  data_s3_prefix: string;
  // VLM/LLM Anomaly Tuning Sample_Export (spec: quality-prompt-tuning,
  // Requirements 2.1, 2.9)
  tuning_sample_export: boolean;
  tuning_sample_retention_days: string;
}

// Sample_Export retention bounds, mirroring the backend
// (functions/tuning_settings.py, Requirement 2.9).
export const TUNING_RETENTION_DEFAULT_DAYS = 30;
export const TUNING_RETENTION_MIN_DAYS = 7;
export const TUNING_RETENTION_MAX_DAYS = 365;

export function validateTuningRetentionDays(value: string): string | undefined {
  const text = (value ?? '').trim();
  if (!text) return undefined; // empty ⇒ the backend default applies
  if (!/^\d+$/.test(text)) {
    return 'Retention must be a whole number of days';
  }
  const days = Number(text);
  if (days < TUNING_RETENTION_MIN_DAYS || days > TUNING_RETENTION_MAX_DAYS) {
    return `Retention must be between ${TUNING_RETENTION_MIN_DAYS} and ${TUNING_RETENTION_MAX_DAYS} days`;
  }
  return undefined;
}

// Use-case bucket configuration (spec: portal-usecase-bucket-config, design
// §5.1 / §10.2): the configured buckets of a record, always allowed.
export function configuredBuckets(useCase: Pick<UseCase, 's3_bucket' | 'data_s3_bucket'>): string[] {
  const names: string[] = [];
  if (useCase.s3_bucket) names.push(useCase.s3_bucket);
  if (useCase.data_s3_bucket && useCase.data_s3_bucket !== useCase.s3_bucket) {
    names.push(useCase.data_s3_bucket);
  }
  return names;
}

/**
 * The additional buckets after a PUT: the server's allowed set minus the
 * configured buckets (the allowed set already excludes the portal's own
 * buckets).
 */
export function additionalFromAllowed(
  response: Pick<UsecaseBucketUpdateResponse, 'allowed_s3_buckets'>,
  useCase: Pick<UseCase, 's3_bucket' | 'data_s3_bucket'>
): string[] {
  const configured = new Set(configuredBuckets(useCase));
  return (response.allowed_s3_buckets || []).filter((name) => !configured.has(name));
}

/**
 * The row actions a caller sees (spec: portal-authorization-and-iam-scoping,
 * design I1 D1 / I3 D3): re-provisioning shared components is a PortalAdmin
 * action, so it is offered only to a PortalAdmin. Cosmetic — the server
 * decides; a stale answer at worst shows an action that answers 403.
 */
export function rowActionsFor(isPortalAdmin: boolean): Array<{ id: string; text: string }> {
  return [
    { id: 'browse', text: 'Browse Datasets' },
    { id: 'team', text: 'Manage Team' },
    ...(isPortalAdmin ? [{ id: 'provision', text: 'Re-provision Shared Components' }] : []),
    { id: 'edit', text: 'Edit' },
    { id: 'delete', text: 'Delete' },
  ];
}

/** Pending marker of the "Allow" action; '+' never occurs in a bucket name. */
const ADD_PENDING = '+add';

/** Message for a failed allow/remove that the notice does not render. */
export function bucketUpdateErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return USECASE_ADMIN_REQUIRED_MESSAGE;
    const code = err.details?.error_code;
    if (code === 'usecase_not_found') return 'This use case no longer exists';
    if (code === 'concurrent_update') {
      return 'The use case was changed by someone else at the same time; retry';
    }
    return err.message;
  }
  return err instanceof Error && err.message ? err.message : 'Failed to update the allowed buckets';
}

export default function UseCases() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  // Use-case creation and re-provisioning are PortalAdmin actions (design I1
  // D1, I3 D3); the server-resolved answer gates their visibility.
  const { isPortalAdmin } = useUsecasePermissions();
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [selectedUseCase, setSelectedUseCase] = useState<UseCase | null>(null);
  const [showTeamModal, setShowTeamModal] = useState(false);
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');
  const [formData, setFormData] = useState<FormDataType>({
    name: '',
    account_id: '',
    s3_bucket: '',
    s3_prefix: '',
    cross_account_role_arn: '',
    data_account_id: '',
    data_account_role_arn: '',
    data_account_external_id: '',
    data_s3_bucket: '',
    data_s3_prefix: '',
    tuning_sample_export: false,
    tuning_sample_retention_days: '',
  });

  // Allowed buckets section of the Edit modal (design §10.2): the additional
  // buckets of the opened record, the name being allowed, and its errors.
  const [additionalBuckets, setAdditionalBuckets] = useState<string[]>([]);
  const [newAllowedBucket, setNewAllowedBucket] = useState('');
  // The bucket name being removed, or ADD_PENDING (not a valid bucket name).
  const [bucketActionPending, setBucketActionPending] = useState<string | null>(null);
  const [bucketActionError, setBucketActionError] = useState('');
  const [bucketNoticeError, setBucketNoticeError] = useState<unknown>(null);
  // Registration_Report remedies (design I5): per bucket allowed in the open
  // Edit modal, and of the last saved edit (shown after the modal closes).
  const [bucketRemedies, setBucketRemedies] = useState<Record<string, string>>({});
  const [saveRegistration, setSaveRegistration] = useState<RegistrationReport | null>(null);

  const resetForm = () => {
    setFormData({
      name: '',
      account_id: '',
      s3_bucket: '',
      s3_prefix: '',
      cross_account_role_arn: '',
      data_account_id: '',
      data_account_role_arn: '',
      data_account_external_id: '',
      data_s3_bucket: '',
      data_s3_prefix: '',
      tuning_sample_export: false,
      tuning_sample_retention_days: '',
    });
    setAdditionalBuckets([]);
    setNewAllowedBucket('');
    setBucketActionPending(null);
    setBucketActionError('');
    setBucketNoticeError(null);
    setBucketRemedies({});
  };

  const { data, isLoading } = useQuery({
    queryKey: ['usecases', user?.user_id],
    queryFn: () => apiService.listUseCases(),
    staleTime: 10000, // Consider data stale after 10 seconds
    refetchInterval: 15000, // Auto-refresh every 15 seconds to catch async provisioning updates
  });

  const { items: sortedUseCases, sortingProps } = useTableSort<UseCase>(data?.usecases || []);

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<UseCase> }) =>
      apiService.updateUseCase(id, data),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['usecases', user?.user_id] });
      setShowEditModal(false);
      resetForm();
      setSaveRegistration(
        registrationRemedies(result?.registration).length > 0 ? result.registration ?? null : null
      );
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
      queryClient.invalidateQueries({ queryKey: ['usecases', user?.user_id] });
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
    onSuccess: (data, usecaseId) => {
      // Update only the specific usecase in cache instead of invalidating all
      queryClient.setQueryData(['usecases', user?.user_id], (oldData: any) => {
        if (!oldData) return oldData;
        return {
          ...oldData,
          usecases: oldData.usecases.map((uc: UseCase) =>
            uc.usecase_id === usecaseId
              ? {
                  ...uc,
                  shared_components_provisioned: true,
                  shared_components: data.components,
                }
              : uc
          ),
        };
      });
      
      // Invalidate status since it might have changed
      queryClient.invalidateQueries({ queryKey: ['shared-components-status', user?.user_id] });
      
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
    queryKey: ['shared-components-status', user?.user_id],
    queryFn: () => apiService.getSharedComponentsStatus(),
    enabled: user?.role === 'PortalAdmin',
  });

  // Mutation for updating all usecases
  const updateAllMutation = useMutation({
    mutationFn: () => apiService.updateAllSharedComponents(),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['shared-components-status', user?.user_id] });
      queryClient.invalidateQueries({ queryKey: ['usecases', user?.user_id] });
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

  const handleEdit = (useCase: UseCase) => {
    setSelectedUseCase(useCase);
    setFormData({
      name: useCase.name,
      account_id: useCase.account_id,
      s3_bucket: useCase.s3_bucket,
      s3_prefix: useCase.s3_prefix || '',
      cross_account_role_arn: useCase.cross_account_role_arn || '',
      data_account_id: useCase.data_account_id || '',
      data_account_role_arn: useCase.data_account_role_arn || '',
      data_account_external_id: useCase.data_account_external_id || '',
      data_s3_bucket: useCase.data_s3_bucket || '',
      data_s3_prefix: useCase.data_s3_prefix || '',
      tuning_sample_export: useCase.tuning_sample_export === true,
      tuning_sample_retention_days:
        useCase.tuning_sample_retention_days !== undefined &&
        useCase.tuning_sample_retention_days !== null
          ? String(useCase.tuning_sample_retention_days)
          : '',
    });
    setAdditionalBuckets(useCase.additional_s3_buckets || []);
    setNewAllowedBucket('');
    setBucketActionError('');
    setBucketNoticeError(null);
    setBucketRemedies({});
    setShowEditModal(true);
  };

  // Allow / remove one additional bucket through PUT /usecases/{id}
  // (design §5.3): the server probes eligibility; its answer carries the
  // allowed set the section is refreshed from.
  const applyBucketUpdate = (response: UsecaseBucketUpdateResponse) => {
    if (!selectedUseCase) return;
    setAdditionalBuckets(additionalFromAllowed(response, selectedUseCase));
    const remedies = registrationRemedies(response.registration);
    if (remedies.length > 0) {
      setBucketRemedies((prev) => {
        const next = { ...prev };
        for (const { bucket, remedy } of remedies) next[bucket] = remedy;
        return next;
      });
    }
    queryClient.invalidateQueries({ queryKey: ['usecases', user?.user_id] });
  };

  const handleAllowBucket = async () => {
    if (!selectedUseCase) return;
    const name = newAllowedBucket.trim();
    if (!name || validateBucketName(name)) return;
    setBucketActionPending(ADD_PENDING);
    setBucketActionError('');
    setBucketNoticeError(null);
    try {
      const response = await apiService.allowBucketForUsecase(selectedUseCase.usecase_id, name);
      applyBucketUpdate(response);
      setNewAllowedBucket('');
    } catch (err) {
      if (bucketErrorCode(err)) {
        setBucketNoticeError(err);
      } else {
        setBucketActionError(bucketUpdateErrorMessage(err));
      }
    } finally {
      setBucketActionPending(null);
    }
  };

  const handleRemoveBucket = async (name: string) => {
    if (!selectedUseCase) return;
    setBucketActionPending(name);
    setBucketActionError('');
    setBucketNoticeError(null);
    try {
      const response = await apiService.removeAllowedBucket(selectedUseCase.usecase_id, name);
      applyBucketUpdate(response);
    } catch (err) {
      if (bucketErrorCode(err)) {
        setBucketNoticeError(err);
      } else {
        setBucketActionError(bucketUpdateErrorMessage(err));
      }
    } finally {
      setBucketActionPending(null);
    }
  };

  const handleUpdate = () => {
    if (!selectedUseCase) return;
    if (!formData.name || !formData.s3_bucket) {
      setError('Name and S3 Bucket are required');
      return;
    }
    const retentionError = validateTuningRetentionDays(
      formData.tuning_sample_retention_days
    );
    if (retentionError) {
      setError(retentionError);
      return;
    }
    // Tuning sample export travels as the typed setting pair the backend
    // validates (quality-prompt-tuning 2.1, 2.9); an empty retention field
    // leaves the stored value (or the 30-day default) alone.
    const { tuning_sample_retention_days: retentionDays, ...rest } = formData;
    const data: Partial<UseCase> = { ...rest };
    if (retentionDays.trim()) {
      data.tuning_sample_retention_days = Number(retentionDays.trim());
    }
    updateMutation.mutate({ id: selectedUseCase.usecase_id, data });
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

      {saveRegistration && (
        <Alert
          type="warning"
          dismissible
          onDismiss={() => setSaveRegistration(null)}
          header="The use case was saved; a bucket needs one more step"
        >
          <RegistrationRemedies report={saveRegistration} />
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
        resizableColumns
        header={
          <Header
            variant="h1"
            description="Manage computer vision use cases across AWS accounts"
            actions={
              user && isPortalAdmin ? (
                <Button 
                  variant="primary" 
                  onClick={() => window.location.href = '/usecases/onboard'}
                >
                  Onboard New Use Case
                </Button>
              ) : null
            }
          >
            Use Cases
          </Header>
        }
        loading={isLoading}
        items={sortedUseCases}
        {...sortingProps}
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
            id: 'region',
            header: 'Region',
            cell: (item: UseCase) => (item as any).region || 'us-east-1',
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
                items={rowActionsFor(isPortalAdmin)}
                onItemClick={({ detail }) => {
                  if (detail.id === 'browse') {
                    navigate(`/data?usecase_id=${item.usecase_id}`);
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

          {(selectedUseCase as any)?.setup_type !== 'single-account' && (
          <>
          <FormField
            label="AWS Account ID"
            description="The UseCase account where SageMaker and Greengrass resources are located"
            stretch
          >
            <Input
              value={formData.account_id}
              onChange={({ detail }) => setFormData({ ...formData, account_id: detail.value })}
              placeholder="123456789012"
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
              placeholder="arn:aws:iam::123456789012:role/DDAPortalAccessRole"
            />
          </FormField>
          </>
          )}

          <FormField
            label="S3 Bucket"
            description="S3 bucket for storing datasets and artifacts"
            errorText={validateBucketName(formData.s3_bucket)}
            stretch
          >
            <Input
              value={formData.s3_bucket}
              onChange={({ detail }) => setFormData({ ...formData, s3_bucket: detail.value })}
              placeholder="my-usecase-bucket"
            />
          </FormField>

          {/* Allowed buckets (spec: portal-usecase-bucket-config, design
              §10.2). Every S3 access the portal performs for the use case is
              bound to the configured buckets plus this list; changes here are
              written immediately through PUT /usecases/{id}, independent of
              "Save Changes". */}
          <Header
            variant="h3"
            description="Browsing, uploads, model import, manifests and transforms can only use these buckets. Configured buckets are always allowed; additional buckets must be tagged dda-portal:managed=true and not belong to another use case. Labeling jobs always use the configured data bucket."
          >
            Allowed buckets
          </Header>

          {bucketActionError && (
            <Alert type="error" dismissible onDismiss={() => setBucketActionError('')}>
              {bucketActionError}
            </Alert>
          )}
          {bucketNoticeError !== null && (
            <BucketNotAllowedNotice
              err={bucketNoticeError}
              usecaseId={selectedUseCase?.usecase_id}
              onAllowed={(response) => {
                setBucketNoticeError(null);
                applyBucketUpdate(response);
              }}
            />
          )}

          <Box>
            <Box variant="awsui-key-label">Configured buckets</Box>
            <SpaceBetween size="xxs">
              {selectedUseCase &&
                configuredBuckets(selectedUseCase).map((name) => (
                  <SpaceBetween key={name} direction="horizontal" size="xs" alignItems="center">
                    <code>{name}</code>
                    {bucketRemedies[name] && <BucketRemedy remedy={bucketRemedies[name]} />}
                  </SpaceBetween>
                ))}
            </SpaceBetween>
          </Box>

          <Box>
            <Box variant="awsui-key-label">Additional buckets</Box>
            {additionalBuckets.length === 0 ? (
              <Box color="text-body-secondary">No additional buckets</Box>
            ) : (
              <SpaceBetween size="xxs">
                {additionalBuckets.map((name) => (
                  <SpaceBetween key={name} direction="horizontal" size="xs" alignItems="center">
                    <code>{name}</code>
                    {bucketRemedies[name] && <BucketRemedy remedy={bucketRemedies[name]} />}
                    <Button
                      variant="inline-link"
                      onClick={() => handleRemoveBucket(name)}
                      loading={bucketActionPending === name}
                      disabled={bucketActionPending !== null}
                      ariaLabel={`Remove ${name}`}
                    >
                      Remove
                    </Button>
                  </SpaceBetween>
                ))}
              </SpaceBetween>
            )}
          </Box>

          <FormField
            label="Allow another bucket"
            description="Bucket name only (no s3:// prefix). The server checks the dda-portal:managed tag before adding it."
            errorText={validateBucketName(newAllowedBucket)}
            stretch
          >
            <SpaceBetween direction="horizontal" size="xs">
              <Input
                value={newAllowedBucket}
                onChange={({ detail }) => setNewAllowedBucket(detail.value)}
                placeholder="another-bucket"
              />
              <Button
                onClick={handleAllowBucket}
                loading={bucketActionPending === ADD_PENDING}
                disabled={
                  bucketActionPending !== null ||
                  !newAllowedBucket.trim() ||
                  !!validateBucketName(newAllowedBucket)
                }
              >
                Allow
              </Button>
            </SpaceBetween>
          </FormField>

          {/* VLM/LLM Anomaly Tuning Sample_Export (spec:
              quality-prompt-tuning, Requirements 2.1, 2.9). Devices export
              nothing until this is enabled AND the devices are redeployed. */}
          <Header variant="h3">Workflow Tuning</Header>

          <FormField
            label="Tuning sample export"
            description="Let devices upload the exact images and verdicts of every anomaly-mode Bedrock/VLM inspection to workflow-tuning/samples/ in this use case's inference results bucket, for prompt tuning. Takes effect the next time the devices are deployed to."
            stretch
          >
            <Toggle
              checked={formData.tuning_sample_export}
              onChange={({ detail }) =>
                setFormData({ ...formData, tuning_sample_export: detail.checked })
              }
            >
              {formData.tuning_sample_export
                ? 'Enabled — devices export tuning samples'
                : 'Disabled — devices export nothing'}
            </Toggle>
          </FormField>

          <FormField
            label="Tuning sample retention (days)"
            description={`How long exported tuning samples are kept before S3 expires them (${TUNING_RETENTION_MIN_DAYS}-${TUNING_RETENTION_MAX_DAYS}; default ${TUNING_RETENTION_DEFAULT_DAYS}).`}
            errorText={validateTuningRetentionDays(formData.tuning_sample_retention_days)}
            stretch
          >
            <Input
              type="number"
              inputMode="numeric"
              value={formData.tuning_sample_retention_days}
              onChange={({ detail }) =>
                setFormData({
                  ...formData,
                  tuning_sample_retention_days: detail.value,
                })
              }
              placeholder={String(TUNING_RETENTION_DEFAULT_DAYS)}
            />
          </FormField>

          {(selectedUseCase as any)?.setup_type !== 'single-account' && (
          <>
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
            errorText={validateBucketName(formData.data_s3_bucket || '')}
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
          </>
          )}
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
