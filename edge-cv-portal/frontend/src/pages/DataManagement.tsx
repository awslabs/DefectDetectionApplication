import { useState, useEffect, useMemo } from 'react';
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
  Cards,
  TextFilter,
  Pagination,
  ColumnLayout,
  Link,
  StatusIndicator,
  Grid,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
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

// Helper to detect folder purpose from path
const getFolderPurpose = (path: string): { label: string; color: 'blue' | 'green' | 'grey' | 'red' } => {
  const lowerPath = path.toLowerCase();
  if (lowerPath.includes('train') || lowerPath.includes('training')) {
    return { label: 'Training Data', color: 'blue' };
  }
  if (lowerPath.includes('label') || lowerPath.includes('annotation')) {
    return { label: 'Labeling', color: 'green' };
  }
  if (lowerPath.includes('test') || lowerPath.includes('validation')) {
    return { label: 'Test/Validation', color: 'grey' };
  }
  if (lowerPath.includes('output') || lowerPath.includes('result')) {
    return { label: 'Output', color: 'red' };
  }
  return { label: 'Data', color: 'grey' };
};

// Helper to get file type icon
const getFileIcon = (_filename: string): 'file' => {
  // All files use the same icon for now
  return 'file';
};

// Helper to check if file is an image
const isImageFile = (filename: string): boolean => {
  const ext = filename.split('.').pop()?.toLowerCase() || '';
  return ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'].includes(ext);
};

export default function DataManagement() {
  const navigate = useNavigate();
  
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
  const [targetAccountInfo, setTargetAccountInfo] = useState<{account: string, hasDataRole: boolean} | null>(null);

  // UI state
  const [filterText, setFilterText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [selectedFiles, setSelectedFiles] = useState<FileItem[]>([]);
  const pageSize = 20;

  // Modal states
  const [showCreateFolder, setShowCreateFolder] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [showBucketSelector, setShowBucketSelector] = useState(false);

  // Form states
  const [newFolderName, setNewFolderName] = useState('');
  const [uploadFiles, setUploadFiles] = useState<UploadFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);

  const currentPrefix = currentPath.join('/') + (currentPath.length > 0 ? '/' : '');

  // Filtered and paginated files
  const filteredFiles = useMemo(() => {
    if (!filterText) return files;
    const lower = filterText.toLowerCase();
    return files.filter(f => f.name.toLowerCase().includes(lower));
  }, [files, filterText]);

  const paginatedFiles = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredFiles.slice(start, start + pageSize);
  }, [filteredFiles, currentPage]);

  const totalPages = Math.ceil(filteredFiles.length / pageSize);

  // Stats
  const stats = useMemo(() => {
    const imageCount = files.filter(f => isImageFile(f.name)).length;
    const totalSize = files.reduce((sum, f) => sum + f.size, 0);
    return { imageCount, totalSize, folderCount: folders.length, fileCount: files.length };
  }, [files, folders]);

  // Load use cases on mount
  useEffect(() => {
    loadUseCases();
  }, []);

  const loadUseCases = async () => {
    try {
      const response = await apiService.listUseCases();
      const useCaseList = response.usecases || [];
      setUseCases(useCaseList);
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
    setSelectedFiles([]);
    try {
      const response = await apiService.listDataFolders(usecaseId, {
        bucket: selectedBucket,
        prefix: currentPrefix,
      });
      setFolders(response.folders);
      setFiles(response.files);
      setCurrentPage(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load folder contents');
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
      const response = await apiService.getBatchUploadUrls(usecaseId, {
        bucket: selectedBucket,
        prefix: currentPrefix,
        files: fileInfos,
      });

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

        setUploadFiles(prev =>
          prev.map(f => (f.file === uploadFile.file ? { ...f, status: 'uploading' as const } : f))
        );

        try {
          const uploadResponse = await fetch(uploadInfo.upload_url, {
            method: 'PUT',
            body: uploadFile.file,
            headers: { 'Content-Type': uploadInfo.content_type },
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
    setFilterText('');
  };

  const navigateUp = () => {
    setCurrentPath(prev => prev.slice(0, -1));
    setFilterText('');
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const handleUseForTraining = () => {
    if (!selectedBucket || !usecaseId) return;
    const s3Path = `s3://${selectedBucket}/${currentPrefix}`;
    navigate(`/training/create?usecase_id=${usecaseId}&data_path=${encodeURIComponent(s3Path)}`);
  };

  const handleUseForLabeling = () => {
    if (!selectedBucket || !usecaseId) return;
    const s3Path = `s3://${selectedBucket}/${currentPrefix}`;
    navigate(`/labeling/create?usecase_id=${usecaseId}&input_path=${encodeURIComponent(s3Path)}`);
  };

  // Build breadcrumb items
  const breadcrumbItems = [
    { text: 'Data', href: '#' },
    ...(selectedBucket ? [{ text: selectedBucket, href: '#' }] : []),
    ...currentPath.map((p, idx) => ({
      text: p,
      href: '#',
      // Store index for navigation
      data: idx,
    })),
  ];

  return (
    <SpaceBetween size="l">
      {/* Header with use case selector */}
      <Header
        variant="h1"
        description="Browse and manage training data, upload images, and organize datasets"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
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
              placeholder="Select use case"
              disabled={useCases.length === 0}
            />
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
          Select a use case to browse and manage data.
        </Alert>
      ) : !selectedBucket ? (
        <Container>
          <SpaceBetween size="m">
            <Box variant="h3">Select a Data Bucket</Box>
            {targetAccountInfo && (
              <Alert type="info">
                {targetAccountInfo.hasDataRole 
                  ? `Data Account: ${targetAccountInfo.account}`
                  : `UseCase Account: ${targetAccountInfo.account} (no separate Data Account configured)`
                }
              </Alert>
            )}
            <Cards
              items={buckets}
              loading={loading}
              loadingText="Loading buckets..."
              cardDefinition={{
                header: item => (
                  <Link fontSize="heading-m" onFollow={() => {
                    setSelectedBucket(item.name);
                    setCurrentPath([]);
                  }}>
                    {item.name}
                  </Link>
                ),
                sections: [
                  {
                    id: 'region',
                    content: item => <Badge>{item.region}</Badge>,
                  },
                  {
                    id: 'status',
                    content: item => item.is_configured 
                      ? <StatusIndicator type="success">Configured</StatusIndicator>
                      : <StatusIndicator type="info">Available</StatusIndicator>,
                  },
                ],
              }}
              empty={
                <Box textAlign="center" padding="l">
                  <SpaceBetween size="s">
                    <Box variant="h4">No data buckets found</Box>
                    <Box color="text-body-secondary">
                      Tag a bucket with <code>dda-portal:managed=true</code> to make it visible here.
                    </Box>
                  </SpaceBetween>
                </Box>
              }
            />
          </SpaceBetween>
        </Container>
      ) : (
        <SpaceBetween size="m">

          {/* Navigation and Stats Bar */}
          <Container>
            <SpaceBetween size="m">
              {/* Breadcrumb navigation */}
              <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                <BreadcrumbGroup
                  items={breadcrumbItems}
                  onFollow={(e) => {
                    e.preventDefault();
                    const text = e.detail.text;
                    if (text === 'Data') {
                      setSelectedBucket(null);
                      setCurrentPath([]);
                    } else if (text === selectedBucket) {
                      setCurrentPath([]);
                    } else {
                      const idx = currentPath.indexOf(text);
                      if (idx >= 0) {
                        setCurrentPath(currentPath.slice(0, idx + 1));
                      }
                    }
                  }}
                />
                <Button variant="icon" iconName="settings" onClick={() => setShowBucketSelector(true)}>
                  Change Bucket
                </Button>
              </SpaceBetween>

              {/* Stats row */}
              <Grid gridDefinition={[{ colspan: 3 }, { colspan: 3 }, { colspan: 3 }, { colspan: 3 }]}>
                <Box>
                  <Box color="text-body-secondary" fontSize="body-s">Folders</Box>
                  <Box fontSize="heading-m">{stats.folderCount}</Box>
                </Box>
                <Box>
                  <Box color="text-body-secondary" fontSize="body-s">Files</Box>
                  <Box fontSize="heading-m">{stats.fileCount}</Box>
                </Box>
                <Box>
                  <Box color="text-body-secondary" fontSize="body-s">Images</Box>
                  <Box fontSize="heading-m">{stats.imageCount}</Box>
                </Box>
                <Box>
                  <Box color="text-body-secondary" fontSize="body-s">Total Size</Box>
                  <Box fontSize="heading-m">{formatFileSize(stats.totalSize)}</Box>
                </Box>
              </Grid>

              {/* Action buttons */}
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="arrow-left" disabled={currentPath.length === 0} onClick={navigateUp}>
                  Back
                </Button>
                <Button iconName="add-plus" onClick={() => setShowCreateFolder(true)}>
                  New Folder
                </Button>
                <Button variant="primary" iconName="upload" onClick={() => setShowUpload(true)}>
                  Upload
                </Button>
                <Button iconName="refresh" onClick={loadFolderContents} loading={loading}>
                  Refresh
                </Button>
                <Box margin={{ left: 'l' }}>
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button 
                      onClick={handleUseForTraining}
                      disabled={stats.imageCount === 0}
                      iconName="external"
                    >
                      Use for Training
                    </Button>
                    <Button 
                      onClick={handleUseForLabeling}
                      disabled={stats.imageCount === 0}
                      iconName="external"
                    >
                      Use for Labeling
                    </Button>
                  </SpaceBetween>
                </Box>
              </SpaceBetween>
            </SpaceBetween>
          </Container>

          {/* Folders as cards */}
          {folders.length > 0 && (
            <Container header={<Header variant="h3" counter={`(${folders.length})`}>Folders</Header>}>
              <ColumnLayout columns={4} variant="text-grid">
                {folders.map(folder => {
                  const purpose = getFolderPurpose(folder.path);
                  return (
                    <Box key={folder.path} padding="s">
                      <SpaceBetween size="xxs">
                        <Button variant="link" onClick={() => navigateToFolder(folder.path)}>
                          <SpaceBetween direction="horizontal" size="xs">
                            <Icon name="folder" />
                            <Box fontWeight="bold">{folder.name}</Box>
                          </SpaceBetween>
                        </Button>
                        <Badge color={purpose.color}>{purpose.label}</Badge>
                      </SpaceBetween>
                    </Box>
                  );
                })}
              </ColumnLayout>
            </Container>
          )}

          {/* Files table with filter */}
          <Container
            header={
              <Header
                variant="h3"
                counter={`(${filteredFiles.length}${filterText ? ` of ${files.length}` : ''})`}
                actions={
                  <TextFilter
                    filteringText={filterText}
                    filteringPlaceholder="Filter files..."
                    onChange={({ detail }) => {
                      setFilterText(detail.filteringText);
                      setCurrentPage(1);
                    }}
                  />
                }
              >
                Files
              </Header>
            }
          >
            <Table
              columnDefinitions={[
                {
                  id: 'name',
                  header: 'Name',
                  cell: item => (
                    <SpaceBetween direction="horizontal" size="xs">
                      <Icon name={getFileIcon(item.name)} />
                      <span>{item.name}</span>
                      {isImageFile(item.name) && <Badge color="blue">Image</Badge>}
                    </SpaceBetween>
                  ),
                  sortingField: 'name',
                  width: '40%',
                },
                {
                  id: 'size',
                  header: 'Size',
                  cell: item => formatFileSize(item.size),
                  sortingField: 'size',
                  width: '15%',
                },
                {
                  id: 'modified',
                  header: 'Last Modified',
                  cell: item => new Date(item.last_modified).toLocaleString(),
                  sortingField: 'last_modified',
                  width: '25%',
                },
                {
                  id: 'type',
                  header: 'Type',
                  cell: item => {
                    const ext = item.name.split('.').pop()?.toUpperCase() || 'FILE';
                    return <Badge color="grey">{ext}</Badge>;
                  },
                  width: '10%',
                },
              ]}
              items={paginatedFiles}
              loading={loading}
              loadingText="Loading files..."
              selectionType="multi"
              selectedItems={selectedFiles}
              onSelectionChange={({ detail }) => setSelectedFiles(detail.selectedItems)}
              sortingDisabled={false}
              empty={
                <Box textAlign="center" padding="l">
                  <SpaceBetween size="s">
                    <Icon name="folder-open" size="big" />
                    <Box variant="h4">
                      {filterText ? 'No matching files' : 'This folder is empty'}
                    </Box>
                    <Box color="text-body-secondary">
                      {filterText 
                        ? 'Try a different search term'
                        : 'Upload images to start building your dataset'
                      }
                    </Box>
                    {!filterText && (
                      <Button variant="primary" onClick={() => setShowUpload(true)}>
                        Upload Files
                      </Button>
                    )}
                  </SpaceBetween>
                </Box>
              }
              pagination={
                totalPages > 1 && (
                  <Pagination
                    currentPageIndex={currentPage}
                    pagesCount={totalPages}
                    onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
                  />
                )
              }
            />
          </Container>
        </SpaceBetween>
      )}

      {/* Bucket Selector Modal */}
      <Modal
        visible={showBucketSelector}
        onDismiss={() => setShowBucketSelector(false)}
        header="Select Data Bucket"
        size="medium"
      >
        <SpaceBetween size="m">
          {targetAccountInfo && (
            <Alert type="info">
              {targetAccountInfo.hasDataRole 
                ? `Showing buckets from Data Account: ${targetAccountInfo.account}`
                : `Showing buckets from UseCase Account: ${targetAccountInfo.account}`
              }
            </Alert>
          )}
          <Table
            columnDefinitions={[
              {
                id: 'name',
                header: 'Bucket',
                cell: item => (
                  <Button variant="link" onClick={() => {
                    setSelectedBucket(item.name);
                    setCurrentPath([]);
                    setShowBucketSelector(false);
                  }}>
                    {item.name}
                  </Button>
                ),
              },
              {
                id: 'region',
                header: 'Region',
                cell: item => <Badge>{item.region}</Badge>,
              },
              {
                id: 'status',
                header: 'Status',
                cell: item => item.is_configured 
                  ? <StatusIndicator type="success">Configured</StatusIndicator>
                  : <StatusIndicator type="info">Available</StatusIndicator>,
              },
            ]}
            items={buckets}
            empty={<Box textAlign="center">No buckets available</Box>}
          />
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
              <Button variant="link" onClick={() => setShowCreateFolder(false)}>Cancel</Button>
              <Button variant="primary" onClick={handleCreateFolder} disabled={!newFolderName.trim()}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormField
          label="Folder Name"
          description={`Location: s3://${selectedBucket}/${currentPrefix}`}
          constraintText="Use lowercase letters, numbers, and hyphens"
        >
          <Input
            value={newFolderName}
            onChange={({ detail }) => setNewFolderName(detail.value)}
            placeholder="e.g., training-images"
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
                Upload {uploadFiles.filter(f => f.status === 'pending').length > 0 && 
                  `(${uploadFiles.filter(f => f.status === 'pending').length} files)`}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info">
            Uploading to: <code>s3://{selectedBucket}/{currentPrefix}</code>
          </Alert>
          
          <FormField label="Select Files" description="Choose images or data files to upload">
            <Box padding="m" textAlign="center" variant="div">
              <input
                type="file"
                multiple
                accept="image/*,.json,.csv,.txt,.manifest"
                onChange={handleFileSelect}
                style={{ display: 'block', margin: '0 auto' }}
              />
              <Box color="text-body-secondary" fontSize="body-s" margin={{ top: 's' }}>
                Supported: Images (JPG, PNG), JSON, CSV, TXT, Manifest files
              </Box>
            </Box>
          </FormField>

          {uploadFiles.length > 0 && (
            <Table
              columnDefinitions={[
                {
                  id: 'name',
                  header: 'File',
                  cell: item => (
                    <SpaceBetween direction="horizontal" size="xs">
                      <Icon name={isImageFile(item.file.name) ? 'file' : 'file'} />
                      <span>{item.file.name}</span>
                    </SpaceBetween>
                  ),
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
                      case 'pending': return <Badge color="grey">Ready</Badge>;
                      case 'uploading': return <Badge color="blue">Uploading...</Badge>;
                      case 'completed': return <Badge color="green">Done</Badge>;
                      case 'error': return <Badge color="red">Failed</Badge>;
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
                      <Box color="text-status-error" fontSize="body-s">{item.error}</Box>
                    ) : null
                  ),
                },
                {
                  id: 'actions',
                  header: '',
                  cell: item => (
                    item.status === 'pending' && !isUploading ? (
                      <Button
                        variant="icon"
                        iconName="close"
                        onClick={() => setUploadFiles(prev => prev.filter(f => f !== item))}
                      />
                    ) : null
                  ),
                },
              ]}
              items={uploadFiles}
            />
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
