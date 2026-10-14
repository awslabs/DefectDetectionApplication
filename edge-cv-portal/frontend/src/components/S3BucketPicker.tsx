/**
 * S3BucketPicker - browse S3 buckets in the current (portal) account and
 * select one via a radio button, or (as a PortalAdmin) create a new bucket
 * with default settings. Selecting (or creating) a bucket calls onSelect(name).
 *
 * Used by the "Onboard New Use Case" flow so users can pick a bucket from a
 * list instead of typing its name. The selected value still flows into the
 * existing s3Bucket form field, so submit behavior is unchanged.
 *
 * Use-case bucket configuration (spec: portal-usecase-bucket-config, design
 * §10.2): each row carries the server's advisory marking — "Tagged for the
 * portal" (the dda-portal:managed=true tag the server requires before a use
 * case can configure the bucket), "Not tagged for the portal", and
 * "Portal-created". Tag discovery is regional, so the server decides again
 * when the use case is created. A bucket created here is tagged for the
 * portal and ABAC-enabled by the server; creating one without a use case is a
 * PortalAdmin operation, so the "Create new bucket" control is rendered only
 * for PortalAdmins and a 403 is still rendered by code if that answer is stale.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Header,
  Input,
  Popover,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { ApiError, apiService } from '../services/api';
import { S3Bucket } from '../types';
import { useUsecasePermissions } from '../hooks/useUsecasePermissions';

interface S3BucketPickerProps {
  selectedBucket: string;
  onSelect: (bucketName: string) => void;
  /** Region used when creating a new bucket (from the onboarding form). */
  region?: string;
}

/** Tooltip of the "Not tagged for the portal" badge (design §10.2). */
export const NOT_TAGGED_TOOLTIP =
  'Tag discovery is regional; the server decides when the use case is created';
export const CREATE_BUCKET_FORBIDDEN_MESSAGE =
  'Only a PortalAdmin can create a bucket here';

/**
 * The message shown for a failed create, by the server's error code: the
 * name is taken (409 `bucket_already_exists`), the name is malformed (400
 * `invalid_bucket_name`), the caller is not a PortalAdmin (403), else the
 * server's message.
 */
export function createBucketErrorMessage(err: unknown, name: string): string {
  if (err instanceof ApiError) {
    const code = err.details?.error_code;
    const remedy = typeof err.details?.remedy === 'string' ? err.details.remedy : '';
    if (code === 'bucket_already_exists') {
      return `Bucket ${name} already exists in this account. ${remedy || 'Select it from the list instead.'}`.trim();
    }
    if (code === 'invalid_bucket_name') {
      return `${name} is not a valid bucket name: use 3–63 lowercase letters, digits, dots and hyphens, starting and ending with a letter or digit.`;
    }
    if (err.status === 403) {
      return CREATE_BUCKET_FORBIDDEN_MESSAGE;
    }
  }
  return err instanceof Error && err.message ? err.message : 'Failed to create bucket';
}

/** Advisory marking of one row from `eligible` / `portal_created`. */
export function BucketEligibilityBadges({ bucket }: { bucket: S3Bucket }): JSX.Element {
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {bucket.eligible === true && <Badge color="green">Tagged for the portal</Badge>}
      {bucket.eligible === false && (
        <Popover
          dismissButton={false}
          position="top"
          size="medium"
          triggerType="custom"
          content={NOT_TAGGED_TOOLTIP}
        >
          <Badge color="grey">Not tagged for the portal</Badge>
        </Popover>
      )}
      {bucket.eligible === undefined && <span>-</span>}
      {bucket.portal_created && <Badge color="blue">Portal-created</Badge>}
    </SpaceBetween>
  );
}

export default function S3BucketPicker({
  selectedBucket,
  onSelect,
  region,
}: S3BucketPickerProps): JSX.Element {
  const [buckets, setBuckets] = useState<S3Bucket[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const { isPortalAdmin } = useUsecasePermissions();

  // Create-new-bucket UI state
  const [creating, setCreating] = useState(false);
  const [newBucketName, setNewBucketName] = useState('');
  const [createInProgress, setCreateInProgress] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const loadBuckets = async () => {
    try {
      setLoading(true);
      setError(null);
      const result = await apiService.listS3Buckets();
      setBuckets(result.buckets || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to list S3 buckets');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadBuckets();
  }, []);

  const handleCreate = async () => {
    const name = newBucketName.trim();
    if (!name) {
      setCreateError('Enter a bucket name');
      return;
    }
    try {
      setCreateInProgress(true);
      setCreateError(null);
      const result = await apiService.createS3Bucket({ name, region });
      const created = result.bucket?.name || name;
      // Refresh the list, select the new bucket, and collapse the create form.
      await loadBuckets();
      onSelect(created);
      setCreating(false);
      setNewBucketName('');
    } catch (err) {
      setCreateError(createBucketErrorMessage(err, name));
    } finally {
      setCreateInProgress(false);
    }
  };

  const filtered = buckets.filter((b) =>
    b.name.toLowerCase().includes(filterText.toLowerCase()),
  );

  const selectedItems = buckets.filter((b) => b.name === selectedBucket);

  return (
    <SpaceBetween size="s">
      <Table
        variant="embedded"
        items={filtered}
        loading={loading}
        loadingText="Loading buckets"
        trackBy="name"
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => {
          const sel = detail.selectedItems[0];
          if (sel) {
            onSelect(sel.name);
          }
        }}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Bucket name',
            cell: (item: S3Bucket) => item.name,
            sortingField: 'name',
          },
          {
            id: 'eligibility',
            header: 'Portal tag',
            cell: (item: S3Bucket) => <BucketEligibilityBadges bucket={item} />,
          },
          {
            id: 'region',
            header: 'Region',
            cell: (item: S3Bucket) => item.region || '-',
            sortingField: 'region',
          },
          {
            id: 'creation_date',
            header: 'Created',
            cell: (item: S3Bucket) =>
              item.creation_date
                ? new Date(item.creation_date).toLocaleDateString()
                : '-',
            sortingField: 'creation_date',
          },
        ]}
        header={
          <Header
            counter={buckets.length ? `(${buckets.length})` : undefined}
            description="A use case can only be configured with a bucket tagged dda-portal:managed=true. Buckets created here are tagged for the portal."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {isPortalAdmin && (
                  <Button
                    iconName="add-plus"
                    onClick={() => {
                      setCreating((c) => !c);
                      setCreateError(null);
                    }}
                  >
                    Create new bucket
                  </Button>
                )}
                <Button iconName="refresh" onClick={loadBuckets} loading={loading}>
                  Refresh
                </Button>
              </SpaceBetween>
            }
          >
            Select an S3 bucket
          </Header>
        }
        filter={
          <TextFilter
            filteringText={filterText}
            filteringPlaceholder="Find bucket"
            onChange={({ detail }) => setFilterText(detail.filteringText)}
          />
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>{error ? 'Could not load buckets' : 'No buckets found'}</b>
            <Box variant="p" color="inherit">
              {error
                ? error
                : 'No S3 buckets exist in this account, or you do not have permission to list them.'}
            </Box>
            <Button onClick={loadBuckets}>Retry</Button>
          </Box>
        }
      />

      {creating && isPortalAdmin && (
        <Box padding={{ top: 'xs' }}>
          <SpaceBetween size="xs">
            {createError && (
              <Alert type="error" dismissible onDismiss={() => setCreateError(null)}>
                {createError}
              </Alert>
            )}
            <SpaceBetween direction="horizontal" size="xs">
              <Input
                value={newBucketName}
                onChange={({ detail }) => setNewBucketName(detail.value)}
                placeholder="new-bucket-name"
                disabled={createInProgress}
              />
              <Button
                variant="primary"
                onClick={handleCreate}
                loading={createInProgress}
              >
                Create
              </Button>
              <Button
                onClick={() => {
                  setCreating(false);
                  setNewBucketName('');
                  setCreateError(null);
                }}
                disabled={createInProgress}
              >
                Cancel
              </Button>
            </SpaceBetween>
            <Box variant="small" color="text-body-secondary">
              Creates a bucket with default settings in
              {region ? ` ${region}` : ' the portal region'}, tagged{' '}
              <code>dda-portal:managed=true</code> for the portal with ABAC enabled.
              Bucket names are globally unique across all AWS accounts.
            </Box>
          </SpaceBetween>
        </Box>
      )}
    </SpaceBetween>
  );
}
