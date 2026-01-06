import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Button,
  Table,
  Select,
  SelectProps,
  Input,
  Modal,
  FormField,
  Alert,
  BreadcrumbGroup,
  Icon,
  ProgressBar,
  Badge,
  Tabs,
  ColumnLayout,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { UseCase } from '../types';

interface Bucket {
  name: string;
  creation_date?: string;
  region: string;
  tags?: Record<string, string>;
  is_configured?: boolean;
}

interface FolderItem {
  name: string;
  path: string;
}

interface FileItem {
  name: string;
  key: string;
  size: number;
  last_modified: string;
}

interface UploadFile {
  file: File;
  progress: number;
  status: 'pending' | 'uploading' | 'completed' | 'error';
  error?: string;
}

export default function DataManagement() {
  // UseCase selection state
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const usecaseId = selectedUseCase?.value || '';

  // State
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const [selectedBucket, setSelectedBucket] = useState<string | null>(null);
  const [currentPath, setCurrentPath] = useState<string[]>([]);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [activeTab, setActiveTab] = useState('browse');
  const [targetAccountInfo, setTargetAccountInfo] = useState<{account: string, hasDataRole: boolean} | null>(null);

  // Modal states
  const [showCreateBucket, setShowCreateBucket] = useState(false);
  const [showCreateFolder, setShowCreateFolder] = useState(false);
  const [showUpload, setShowUpload] = useState(false);

  // Form states
  const [newBucketName, setNewBucketName] = useState('');
  const [newBucketRegion, setNewBucketRegion] = useState('us-east-1');
  const [newFolderName, setNewFolderName] = useState('');
  const [uploadFiles, setUploadFiles] = useState<UploadFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);

  const currentPrefix = currentPath.join('/') + (currentPath.length > 0 ? '/' : '');

  // Load use cases on mount
  useEffect(() => {
    loadUseCases();
  }, []);

  const loadUseCases = async () => {
    try {
      const response = await apiService.listUseCases();
      const useCaseList = response.usecases || [];
      setUseCases(useCaseList);
      // Auto-select first use case if available
      if (useCaseList.length > 0) {
        setSelectedUseCase({
          label: useCaseList[0].name,
          value: useCaseList[0].usecase_id,
        });
      }
    } catch (err) {
      console.error('Failed to load use cases:', err);
      setLoading(false);
    }
  };

  // Load buckets when usecase changes
  useEffect(() => {
    if (usecaseId) {
      loadBuckets();
    } else {
      setBuckets([]);
      setFolders([]);
      setFiles([]);
      setSelectedBucket(null);
      setLoading(false);
    }
  }, [usecaseId]);

  // Load folder contents when bucket or path changes
  useEffect(() => {
    if (selectedBucket) {
      loadFolderContents();
    }
  }, [selectedBucket, currentPath]);

  const loadBuckets = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiService.listDataBuckets(usecaseId);
      setBuckets(response.buckets);
      
      // Track which account is being queried
      if (response.target_account) {
        setTargetAccountInfo({
          account: response.target_account,
          hasDataRole: response.has_data_account_role || false
        });
      }
      
      if (response.current_data_bucket) {
        setSelectedBucket(response.current_data_bucket);
      } else if (response.buckets.length > 0) {
        setSelectedBucket(response.buckets[0].name);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load buckets');
    } finally {
      setLoading(false);
    }
  };

  const loadFolderContents = async () => {
    if (!selectedBucket) return;
    setLoading(true);
    try {
      const response = await apiService.listDataFolders(usecaseId, {
        bucket: selectedBucket,
        prefix: currentPrefix,
      });
      setFolders(response.folders);
      setFiles(response.files);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load folder contents');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateBucket = async () => {
    if (!newBucketName.trim()) return;
    setLoading(true);
    try {
      await apiService.createDataBucket(usecaseId, {
        bucket_name: newBucketName.toLowerCase(),
        region: newBucketRegion,
        enable_versioning: true,
        encryption: 'AES256',
      });
      setShowCreateBucket(false);
      setNewBucketName('');
      await loadBuckets();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create bucket');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateFolder = async () => {
    if (!newFolderName.trim() || !selectedBucket) return;
    setLoading(true);
    try {
      await apiService.createDataFolder(usecaseId, {
        bucket: selectedBucket,
        folder_path: currentPrefix + newFolderName,
      });
      setShowCreateFolder(false);
      setNewFolderName('');
      await loadFolderContents();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create folder');
    } finally {
      setLoading(false);
    }
  };


  const handleFileSelect = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = event.target.files;
    if (!selectedFiles) return;

    const newUploadFiles: UploadFile[] = Array.from(selectedFiles).map(file => ({
      file,
      progress: 0,
      status: 'pending' as const,
    }));
    setUploadFiles(prev => [...prev, ...newUploadFiles]);
  };

  const handleUpload = async () => {
    if (uploadFiles.length === 0 || !selectedBucket) return;
    setIsUploading(true);

    const pendingFiles = uploadFiles.filter(f => f.status === 'pending');
    const fileInfos = pendingFiles.map(f => ({
      filename: f.file.name,
      content_type: f.file.type || 'application/octet-stream',
    }));

    try {
      // Get presigned URLs for all files
      const response = await apiService.getBatchUploadUrls(usecaseId, {
        bucket: selectedBucket,
        prefix: currentPrefix,
        files: fileInfos,
      });

      // Upload each file
      for (let i = 0; i < pendingFiles.length; i++) {
        const uploadFile = pendingFiles[i];
        const uploadInfo = response.uploads.find(u => u.filename === uploadFile.file.name);

        if (!uploadInfo || uploadInfo.error) {
          setUploadFiles(prev =>
            prev.map(f =>
              f.file === uploadFile.file
                ? { ...f, status: 'error' as const, error: uploadInfo?.error || 'Failed to get upload URL' }
                : f
            )
          );
          continue;
        }

        // Update status to uploading
        setUploadFiles(prev =>
          prev.map(f => (f.file === uploadFile.file ? { ...f, status: 'uploading' as const } : f))
        );

        try {
          // Upload to S3 using presigned URL
          const uploadResponse = await fetch(uploadInfo.upload_url, {
            method: 'PUT',
            body: uploadFile.file,
            headers: {
              'Content-Type': uploadInfo.content_type,
            },
          });

          if (uploadResponse.ok) {
            setUploadFiles(prev =>
              prev.map(f =>
                f.file === uploadFile.file ? { ...f, status: 'completed' as const, progress: 100 } : f
              )
            );
          } else {
            throw new Error(`Upload failed: ${uploadResponse.statusText}`);
          }
        } catch (uploadErr) {
          setUploadFiles(prev =>
            prev.map(f =>
              f.file === uploadFile.file
                ? { ...f, status: 'error' as const, error: uploadErr instanceof Error ? uploadErr.message : 'Upload failed' }
                : f
            )
          );
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload files');
    } finally {
      setIsUploading(false);
      await loadFolderContents();
    }
  };

  const navigateToFolder = (folderPath: string) => {
    const pathParts = folderPath.split('/').filter(p => p);
    setCurrentPath(pathParts);
  };

  const navigateUp = () => {
    setCurrentPath(prev => prev.slice(0, -1));
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const bucketOptions = buckets.map(b => ({ label: b.name, value: b.name }));


  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <Box variant="span">Use Case:</Box>
            <Select
              selectedOption={selectedUseCase}
              onChange={({ detail }) => {
                setSelectedUseCase(detail.selectedOption);
                setSelectedBucket(null);
                setCurrentPath([]);
                setBuckets([]);
                setFolders([]);
                setFiles([]);
              }}
              options={useCases.map((uc) => ({
                label: uc.name,
                value: uc.usecase_id,
              }))}
              placeholder="Select a use case"
              disabled={useCases.length === 0}
              expandToViewport
            />
          </SpaceBetween>
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setShowCreateBucket(true)} disabled={!usecaseId}>
              Create Bucket
            </Button>
          </SpaceBetween>
        }
      >
        Data Management
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {!usecaseId ? (
        <Alert type="info">
          Select a use case above to manage data buckets and upload files.
        </Alert>
      ) : (
        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          tabs={[
            {
              id: 'browse',
              label: 'Browse Files',
              content: (
                <SpaceBetween size="m">
                  <Container>
                    <SpaceBetween size="m">
                      <ColumnLayout columns={2}>
                        <FormField label="Select Bucket">
                          <Select
                            selectedOption={selectedBucket ? { label: selectedBucket, value: selectedBucket } : null}
                            onChange={({ detail }) => {
                              setSelectedBucket(detail.selectedOption.value || null);
                              setCurrentPath([]);
                            }}
                            options={bucketOptions}
                            placeholder="Select a bucket"
                            disabled={loading || buckets.length === 0}
                          />
                        </FormField>
                        <Box>
                          <FormField label="Current Path">
                            <BreadcrumbGroup
                              items={[
                                { text: selectedBucket || 'Root', href: '#' },
                                ...currentPath.map((p) => ({
                                  text: p,
                                  href: '#',
                                })),
                              ]}
                              onFollow={(e: { preventDefault: () => void; detail: { text: string } }) => {
                                e.preventDefault();
                                const index = currentPath.indexOf(e.detail.text);
                                if (index >= 0) {
                                  setCurrentPath(currentPath.slice(0, index + 1));
                                } else {
                                  setCurrentPath([]);
                                }
                              }}
                            />
                          </FormField>
                        </Box>
                      </ColumnLayout>

                      <SpaceBetween direction="horizontal" size="xs">
                        <Button
                          iconName="arrow-left"
                          disabled={currentPath.length === 0}
                          onClick={navigateUp}
                        >
                          Back
                        </Button>
                        <Button onClick={() => setShowCreateFolder(true)} disabled={!selectedBucket}>
                          New Folder
                        </Button>
                        <Button variant="primary" onClick={() => setShowUpload(true)} disabled={!selectedBucket}>
                          Upload Files
                        </Button>
                        <Button iconName="refresh" onClick={loadFolderContents} disabled={!selectedBucket}>
                          Refresh
                        </Button>
                      </SpaceBetween>
                    </SpaceBetween>
                  </Container>


                  {/* Folders */}
                  {folders.length > 0 && (
                    <Container header={<Header variant="h3">Folders</Header>}>
                      <Table
                        columnDefinitions={[
                          {
                            id: 'name',
                            header: 'Name',
                            cell: item => (
                              <Button
                                variant="link"
                                onClick={() => navigateToFolder(item.path)}
                              >
                                <SpaceBetween direction="horizontal" size="xs">
                                  <Icon name="folder" />
                                  <span>{item.name}</span>
                                </SpaceBetween>
                              </Button>
                            ),
                          },
                          {
                            id: 'path',
                            header: 'Path',
                            cell: item => <Box color="text-body-secondary">{item.path}</Box>,
                          },
                        ]}
                        items={folders}
                        loading={loading}
                        loadingText="Loading folders..."
                        empty={<Box textAlign="center">No folders</Box>}
                      />
                    </Container>
                  )}

                  {/* Files */}
                  <Container header={<Header variant="h3">Files ({files.length})</Header>}>
                    <Table
                      columnDefinitions={[
                        {
                          id: 'name',
                          header: 'Name',
                          cell: item => (
                            <SpaceBetween direction="horizontal" size="xs">
                              <Icon name="file" />
                              <span>{item.name}</span>
                            </SpaceBetween>
                          ),
                          sortingField: 'name',
                        },
                        {
                          id: 'size',
                          header: 'Size',
                          cell: item => formatFileSize(item.size),
                          sortingField: 'size',
                        },
                        {
                          id: 'modified',
                          header: 'Last Modified',
                          cell: item => new Date(item.last_modified).toLocaleString(),
                          sortingField: 'last_modified',
                        },
                      ]}
                      items={files}
                      loading={loading}
                      loadingText="Loading files..."
                      empty={
                        <Box textAlign="center" color="inherit">
                          <b>No files</b>
                          <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                            This folder is empty. Upload files to get started.
                          </Box>
                        </Box>
                      }
                    />
                  </Container>
                </SpaceBetween>
              ),
            },
            {
              id: 'buckets',
              label: 'Buckets',
              content: (
                <SpaceBetween size="m">
                  {targetAccountInfo && (
                    <Alert type={targetAccountInfo.hasDataRole ? "info" : "warning"}>
                      {targetAccountInfo.hasDataRole 
                        ? `Querying Data Account: ${targetAccountInfo.account}. Only buckets tagged with dda-portal:managed=true are shown.`
                        : `No separate Data Account configured. Querying UseCase Account: ${targetAccountInfo.account}. To use a separate Data Account, update the use case configuration.`
                      }
                    </Alert>
                  )}
                  <Container>
                    <Table
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Bucket Name',
                        cell: item => (
                          <Button variant="link" onClick={() => {
                            setSelectedBucket(item.name);
                            setCurrentPath([]);
                            setActiveTab('browse');
                          }}>
                            {item.name}
                          </Button>
                        ),
                        sortingField: 'name',
                      },
                      {
                        id: 'region',
                        header: 'Region',
                        cell: item => <Badge>{item.region}</Badge>,
                      },
                      {
                        id: 'tags',
                        header: 'Tags',
                        cell: item => item.is_configured ? <Badge color="green">Configured</Badge> : 
                          Object.keys(item.tags || {}).length > 0 ? <Badge>{Object.keys(item.tags || {}).length} tags</Badge> : '-',
                      },
                    ]}
                    items={buckets}
                    loading={loading}
                    loadingText="Loading buckets..."
                    empty={
                      <Box textAlign="center" color="inherit">
                        <b>No buckets</b>
                        <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                          Create a bucket to store your training data.
                        </Box>
                        <Button onClick={() => setShowCreateBucket(true)}>Create Bucket</Button>
                      </Box>
                    }
                  />
                  </Container>
                </SpaceBetween>
              ),
            },
          ]}
        />
      )}


      {/* Create Bucket Modal */}
      <Modal
        visible={showCreateBucket}
        onDismiss={() => setShowCreateBucket(false)}
        header="Create New Bucket"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowCreateBucket(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleCreateBucket} disabled={!newBucketName.trim()}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label="Bucket Name"
            description="Must be globally unique, 3-63 characters, lowercase letters, numbers, and hyphens only"
          >
            <Input
              value={newBucketName}
              onChange={({ detail }) => setNewBucketName(detail.value.toLowerCase())}
              placeholder="my-training-data-bucket"
            />
          </FormField>
          <FormField label="Region">
            <Select
              selectedOption={{ label: newBucketRegion, value: newBucketRegion }}
              onChange={({ detail }) => setNewBucketRegion(detail.selectedOption.value || 'us-east-1')}
              options={[
                { label: 'us-east-1', value: 'us-east-1' },
                { label: 'us-west-2', value: 'us-west-2' },
                { label: 'eu-west-1', value: 'eu-west-1' },
                { label: 'ap-southeast-1', value: 'ap-southeast-1' },
              ]}
            />
          </FormField>
          <Alert type="info">
            The bucket will be created with versioning enabled and AES-256 encryption.
          </Alert>
        </SpaceBetween>
      </Modal>

      {/* Create Folder Modal */}
      <Modal
        visible={showCreateFolder}
        onDismiss={() => setShowCreateFolder(false)}
        header="Create New Folder"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowCreateFolder(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleCreateFolder} disabled={!newFolderName.trim()}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormField
          label="Folder Name"
          description={`Will be created at: ${selectedBucket}/${currentPrefix}`}
        >
          <Input
            value={newFolderName}
            onChange={({ detail }) => setNewFolderName(detail.value)}
            placeholder="new-folder"
          />
        </FormField>
      </Modal>


      {/* Upload Files Modal */}
      <Modal
        visible={showUpload}
        onDismiss={() => {
          if (!isUploading) {
            setShowUpload(false);
            setUploadFiles([]);
          }
        }}
        header="Upload Files"
        size="large"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowUpload(false);
                  setUploadFiles([]);
                }}
                disabled={isUploading}
              >
                {uploadFiles.some(f => f.status === 'completed') ? 'Done' : 'Cancel'}
              </Button>
              <Button
                variant="primary"
                onClick={handleUpload}
                disabled={uploadFiles.filter(f => f.status === 'pending').length === 0 || isUploading}
                loading={isUploading}
              >
                Upload
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label="Select Files"
            description={`Files will be uploaded to: s3://${selectedBucket}/${currentPrefix}`}
          >
            <input
              type="file"
              multiple
              onChange={handleFileSelect}
              style={{ marginBottom: '16px' }}
            />
          </FormField>

          {uploadFiles.length > 0 && (
            <Table
              columnDefinitions={[
                {
                  id: 'name',
                  header: 'File Name',
                  cell: item => item.file.name,
                },
                {
                  id: 'size',
                  header: 'Size',
                  cell: item => formatFileSize(item.file.size),
                },
                {
                  id: 'status',
                  header: 'Status',
                  cell: item => {
                    switch (item.status) {
                      case 'pending':
                        return <Badge color="grey">Pending</Badge>;
                      case 'uploading':
                        return <Badge color="blue">Uploading...</Badge>;
                      case 'completed':
                        return <Badge color="green">Completed</Badge>;
                      case 'error':
                        return <Badge color="red">Error: {item.error}</Badge>;
                    }
                  },
                },
                {
                  id: 'progress',
                  header: 'Progress',
                  cell: item => (
                    item.status === 'uploading' ? (
                      <ProgressBar value={item.progress} />
                    ) : item.status === 'completed' ? (
                      <Icon name="status-positive" variant="success" />
                    ) : item.status === 'error' ? (
                      <Icon name="status-negative" variant="error" />
                    ) : null
                  ),
                },
                {
                  id: 'actions',
                  header: 'Actions',
                  cell: item => (
                    item.status === 'pending' && !isUploading ? (
                      <Button
                        variant="icon"
                        iconName="remove"
                        onClick={() => setUploadFiles(prev => prev.filter(f => f !== item))}
                      />
                    ) : null
                  ),
                },
              ]}
              items={uploadFiles}
              empty={<Box textAlign="center">No files selected</Box>}
            />
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
