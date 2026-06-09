import { useState, useEffect } from 'react';
import {
  Modal,
  Box,
  SpaceBetween,
  Button,
  Table,
  BreadcrumbGroup,
  Spinner,
  Alert,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface S3BrowseItem {
  name: string;
  key?: string;
  prefix?: string;
  type: 'folder' | 'file' | 'manifest' | 'image';
  size?: number;
  size_mb?: number;
  last_modified?: string;
  s3_uri?: string;
}

interface S3BrowseResult {
  bucket: string;
  current_prefix: string;
  breadcrumbs: Array<{ name: string; prefix: string }>;
  folders: S3BrowseItem[];
  files: S3BrowseItem[];
  folder_count: number;
  file_count: number;
}

interface S3BrowserProps {
  visible: boolean;
  onDismiss: () => void;
  usecaseId: string;
  /** Called when a file is selected (file-selection mode). */
  onSelectFile?: (s3Uri: string) => void;
  /**
   * Called when a folder is selected (folder-selection mode). When provided,
   * each folder gets a "Select Folder" action and a "Select current folder"
   * button is shown for the location currently being browsed.
   */
  onSelectFolder?: (s3Uri: string) => void;
  fileFilter?: (item: S3BrowseItem) => boolean;
  title?: string;
  selectButtonText?: string;
}

export default function S3Browser({
  visible,
  onDismiss,
  usecaseId,
  onSelectFile,
  onSelectFolder,
  fileFilter,
  title = 'Browse S3 Bucket',
  selectButtonText = 'Select',
}: S3BrowserProps) {
  const [browsingS3, setBrowsingS3] = useState(false);
  const [s3Browse, setS3Browse] = useState<S3BrowseResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const folderMode = !!onSelectFolder;

  // Load initial bucket contents when modal opens
  useEffect(() => {
    if (visible && !s3Browse) {
      browseS3Bucket('');
    }
  }, [visible]);

  const browseS3Bucket = async (prefix: string = '') => {
    if (!usecaseId) {
      setError('No use case selected');
      return;
    }

    try {
      setBrowsingS3(true);
      setError(null);

      const result = await apiService.browseS3Bucket(usecaseId, prefix);
      setS3Browse(result as any);
    } catch (err: any) {
      setError('Failed to browse S3 bucket');
      console.error('Browse error:', err);
    } finally {
      setBrowsingS3(false);
    }
  };

  const handleSelectFile = (item: S3BrowseItem) => {
    if (onSelectFile && item.s3_uri) {
      onSelectFile(item.s3_uri);
      onDismiss();
    }
  };

  // Folders don't carry an s3_uri from the backend, so build it from the
  // bucket + folder prefix. Always ends with a trailing slash.
  const folderUri = (prefix: string): string =>
    `s3://${s3Browse?.bucket}/${prefix}`;

  const handleSelectFolder = (prefix: string) => {
    if (onSelectFolder && s3Browse?.bucket && prefix) {
      onSelectFolder(folderUri(prefix));
      onDismiss();
    }
  };

  const getFilteredFiles = () => {
    if (!s3Browse) return [];
    if (!fileFilter) return s3Browse.files;
    return s3Browse.files.filter(fileFilter);
  };

  const currentPrefix = s3Browse?.current_prefix || '';

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={title}
      size="large"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            {folderMode && (
              <Button
                variant="primary"
                disabled={!currentPrefix}
                onClick={() => handleSelectFolder(currentPrefix)}
              >
                {currentPrefix
                  ? `Select current folder (${currentPrefix})`
                  : 'Select current folder'}
              </Button>
            )}
            <Button onClick={onDismiss}>Close</Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {folderMode && (
          <Alert type="info">
            Navigate into a folder and click its <strong>Select Folder</strong>{' '}
            action, or open it and use <strong>Select current folder</strong> below.
          </Alert>
        )}

        {s3Browse && (
          <>
            <Box>
              <strong>Bucket:</strong> {s3Browse.bucket}
            </Box>

            {s3Browse.breadcrumbs.length > 0 && (
              <BreadcrumbGroup
                items={s3Browse.breadcrumbs.map((bc) => ({
                  text: bc.name,
                  // Encode the prefix in href so the group-level onClick can
                  // navigate. Cloudscape ignores per-item onClick handlers.
                  href: `#${encodeURIComponent(bc.prefix)}`,
                }))}
                onClick={(e) => {
                  e.preventDefault();
                  const prefix = decodeURIComponent(
                    (e.detail.href || '#').slice(1),
                  );
                  browseS3Bucket(prefix);
                }}
              />
            )}

            {browsingS3 ? (
              <Box textAlign="center">
                <Spinner />
              </Box>
            ) : (
              <>
                {s3Browse.folders.length > 0 && (
                  <Box>
                    <strong>Folders:</strong>
                    <Table
                      columnDefinitions={[
                        {
                          id: 'name',
                          header: 'Name',
                          cell: (item: S3BrowseItem) => (
                            <Button
                              variant="link"
                              onClick={() => browseS3Bucket(item.prefix!)}
                            >
                              📁 {item.name}
                            </Button>
                          ),
                        },
                        ...(folderMode
                          ? [
                              {
                                id: 'action',
                                header: 'Action',
                                cell: (item: S3BrowseItem) => (
                                  <Button
                                    variant="link"
                                    onClick={() => handleSelectFolder(item.prefix!)}
                                  >
                                    {selectButtonText}
                                  </Button>
                                ),
                              },
                            ]
                          : []),
                      ]}
                      items={s3Browse.folders}
                      variant="embedded"
                    />
                  </Box>
                )}

                {getFilteredFiles().length > 0 && (
                  <Box>
                    <strong>Files:</strong>
                    <Table
                      columnDefinitions={[
                        {
                          id: 'name',
                          header: 'Name',
                          cell: (item: S3BrowseItem) => (
                            <Box>
                              {item.type === 'manifest' && '📄'}
                              {item.type === 'image' && '🖼️'}
                              {item.type === 'file' && '📋'}
                              {' '}
                              {item.name}
                            </Box>
                          ),
                        },
                        {
                          id: 'size',
                          header: 'Size',
                          cell: (item: S3BrowseItem) =>
                            item.size_mb ? `${item.size_mb} MB` : '-',
                        },
                        {
                          id: 'modified',
                          header: 'Modified',
                          cell: (item: S3BrowseItem) =>
                            item.last_modified
                              ? new Date(item.last_modified).toLocaleDateString()
                              : '-',
                        },
                        ...(onSelectFile
                          ? [
                              {
                                id: 'action',
                                header: 'Action',
                                cell: (item: S3BrowseItem) => (
                                  <Button
                                    variant="link"
                                    onClick={() => handleSelectFile(item)}
                                  >
                                    {selectButtonText}
                                  </Button>
                                ),
                              },
                            ]
                          : []),
                      ]}
                      items={getFilteredFiles()}
                      variant="embedded"
                    />
                  </Box>
                )}

                {s3Browse.folders.length === 0 && getFilteredFiles().length === 0 && (
                  <Alert type="info">
                    {folderMode
                      ? 'This folder has no subfolders. Use "Select current folder" below to choose this location.'
                      : 'No files or folders found in this location'}
                  </Alert>
                )}
              </>
            )}
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
}
