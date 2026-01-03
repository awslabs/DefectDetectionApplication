import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Table,
  Button,
  SpaceBetween,
  Box,
  Badge,
  Alert,
  Popover,
} from '@cloudscape-design/components';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { S3Dataset } from '../types';
import { apiService } from '../services/api';
import ImagePreview from '../components/ImagePreview';

export default function DatasetBrowser() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const useCaseId = searchParams.get('usecase_id') || '';
  
  const [datasets, setDatasets] = useState<S3Dataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedItems, setSelectedItems] = useState<S3Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [bucket, setBucket] = useState('');
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewPrefix, setPreviewPrefix] = useState('');

  useEffect(() => {
    if (useCaseId) {
      loadDatasets();
    } else {
      setError('No use case selected');
      setLoading(false);
    }
  }, [useCaseId]);

  const loadDatasets = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiService.listDatasets({
        usecase_id: useCaseId,
        max_depth: 3,
      });
      
      setBucket(response.bucket);
      
      // Transform API response to match S3Dataset type
      const transformedDatasets: S3Dataset[] = response.datasets.map(d => ({
        prefix: d.prefix,
        image_count: d.image_count,
        last_modified: d.last_modified ? new Date(d.last_modified).getTime() : Date.now(),
      }));
      
      setDatasets(transformedDatasets);
    } catch (err) {
      console.error('Failed to load datasets:', err);
      setError(err instanceof Error ? err.message : 'Failed to load datasets');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateJob = () => {
    if (selectedItems.length > 0) {
      navigate('/labeling/create', { state: { dataset: selectedItems[0] } });
    }
  };

  const handlePreviewImages = (prefix: string) => {
    setPreviewPrefix(prefix);
    setPreviewVisible(true);
  };

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={bucket ? `Browse datasets in ${bucket}` : 'Browse datasets'}
          info={
            <Popover
              dismissButton={false}
              position="top"
              size="large"
              triggerType="custom"
              content={
                <SpaceBetween size="s">
                  <Box variant="h4">How to use this page:</Box>
                  <ul>
                    <li><strong>Select a dataset</strong> - Click on any row to select a dataset prefix</li>
                    <li><strong>Create labeling job</strong> - After selecting a dataset, click "Create Labeling Job with Selected Dataset" to start labeling images</li>
                    <li><strong>View details</strong> - Each dataset shows the S3 prefix path, number of images, and last modified date</li>
                  </ul>
                  <Box variant="h4">Dataset requirements:</Box>
                  <ul>
                    <li>Datasets must contain image files (.jpg, .jpeg, .png, .bmp, .tiff)</li>
                    <li>Images should be organized in S3 prefixes (folders)</li>
                    <li>The bucket must be tagged with <code>dda-portal:managed=true</code></li>
                  </ul>
                  <Box variant="small" color="text-body-secondary">
                    Tip: Select datasets with sufficient images for effective model training (typically 100+ images per class).
                  </Box>
                </SpaceBetween>
              }
            >
              <Button variant="icon" iconName="status-info" />
            </Popover>
          }
          actions={
            <Button onClick={() => navigate('/labeling')}>
              Back to Labeling Jobs
            </Button>
          }
        >
          Dataset Browser
        </Header>
      }
    >
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      
      {!loading && datasets.length === 0 && !error && (
        <Alert type="info" header="No datasets found">
          <SpaceBetween size="s">
            <Box>
              No image datasets were found in the S3 bucket. This could be because:
            </Box>
            <ul>
              <li>The bucket is empty or contains no image files (.jpg, .jpeg, .png, .bmp, .tiff)</li>
              <li>The bucket is not tagged for portal access</li>
            </ul>
            <Box variant="h4">To fix bucket access:</Box>
            <Box padding="s">
              <code style={{ display: 'block', padding: '8px', backgroundColor: '#f4f4f4', borderRadius: '4px' }}>
                aws s3api put-bucket-tagging --bucket {bucket || 'YOUR_BUCKET'} --tagging 'TagSet=[{`{Key=dda-portal:managed,Value=true}`}]'
              </code>
            </Box>
            <Box variant="small" color="text-body-secondary">
              After tagging the bucket, refresh this page to see your datasets.
            </Box>
          </SpaceBetween>
        </Alert>
      )}
      
      <Table
        columnDefinitions={[
          {
            id: 'prefix',
            header: 'S3 Prefix',
            cell: (item) => (
              <Box>
                <Box fontWeight="bold">{item.prefix}</Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  s3://{bucket}/{item.prefix}
                </Box>
              </Box>
            ),
            sortingField: 'prefix',
          },
          {
            id: 'image_count',
            header: 'Image Count',
            cell: (item) => (
              <Badge color="blue">{item.image_count.toLocaleString()} images</Badge>
            ),
            sortingField: 'image_count',
          },
          {
            id: 'last_modified',
            header: 'Last Modified',
            cell: (item) => new Date(item.last_modified).toLocaleString(),
            sortingField: 'last_modified',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item) => (
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="normal"
                  iconName="view-horizontal"
                  onClick={() => handlePreviewImages(item.prefix)}
                  disabled={item.image_count === 0}
                >
                  Preview Images
                </Button>
              </SpaceBetween>
            ),
          },
        ]}
        items={datasets}
        loading={loading}
        loadingText="Loading datasets"
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) =>
          setSelectedItems(detail.selectedItems)
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>No datasets found</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              No image datasets found in the S3 bucket for this use case.
            </Box>
          </Box>
        }
        sortingDisabled={false}
        footer={
          selectedItems.length > 0 && (
            <Box textAlign="center" padding="s">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setSelectedItems([])}>Clear Selection</Button>
                <Button variant="primary" onClick={handleCreateJob}>
                  Create Labeling Job with Selected Dataset
                </Button>
              </SpaceBetween>
            </Box>
          )
        }
      />

      <ImagePreview
        visible={previewVisible}
        onDismiss={() => setPreviewVisible(false)}
        usecaseId={useCaseId}
        prefix={previewPrefix}
        datasetName={previewPrefix}
      />
    </Container>
  );
}
