/**
 * S3BucketPicker - browse S3 buckets in the current (portal) account and
 * select one via a radio button, or create a new bucket with default settings.
 * Selecting (or creating) a bucket calls onSelect(name).
 *
 * Used by the "Onboard New Use Case" flow so users can pick a bucket from a
 * list instead of typing its name. The selected value still flows into the
 * existing s3Bucket form field, so submit behavior is unchanged.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Header,
  Input,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { S3Bucket } from '../types';

interface S3BucketPickerProps {
  selectedBucket: string;
  onSelect: (bucketName: string) => void;
  /** Region used when creating a new bucket (from the onboarding form). */
  region?: string;
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
      setCreateError(err instanceof Error ? err.message : 'Failed to create bucket');
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
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="add-plus"
                  onClick={() => {
                    setCreating((c) => !c);
                    setCreateError(null);
                  }}
                >
                  Create new bucket
                </Button>
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

      {creating && (
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
              {region ? ` ${region}` : ' the portal region'}. Bucket names are
              globally unique across all AWS accounts.
            </Box>
          </SpaceBetween>
        </Box>
      )}
    </SpaceBetween>
  );
}
