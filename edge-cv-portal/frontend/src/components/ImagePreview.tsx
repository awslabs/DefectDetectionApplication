import { useState, useEffect } from 'react';
import {
  Modal,
  Box,
  SpaceBetween,
  Button,
  Alert,
  Grid,
  Container,
  Header,
  Badge,
  Spinner,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface ImagePreviewProps {
  visible: boolean;
  onDismiss: () => void;
  usecaseId: string;
  prefix: string;
  datasetName?: string;
}

interface PreviewImage {
  key: string;
  filename: string;
  size: number;
  last_modified: string;
  presigned_url: string;
}

export default function ImagePreview({
  visible,
  onDismiss,
  usecaseId,
  prefix,
  datasetName,
}: ImagePreviewProps) {
  const [images, setImages] = useState<PreviewImage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedImage, setSelectedImage] = useState<PreviewImage | null>(null);
  const [totalFound, setTotalFound] = useState(0);

  useEffect(() => {
    if (visible && usecaseId && prefix) {
      loadImages();
    }
  }, [visible, usecaseId, prefix]);

  const loadImages = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiService.getImagePreview({
        usecase_id: usecaseId,
        prefix: prefix,
        limit: 12, // Load up to 12 images for preview
      });
      
      setImages(response.images);
      setTotalFound(response.total_found);
    } catch (err) {
      console.error('Failed to load image preview:', err);
      setError(err instanceof Error ? err.message : 'Failed to load images');
    } finally {
      setLoading(false);
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  return (
    <>
      <Modal
        onDismiss={onDismiss}
        visible={visible}
        size="max"
        header={
          <Header
            variant="h2"
            description={`Preview images from ${prefix}`}
            actions={
              <Button onClick={onDismiss}>Close</Button>
            }
          >
            {datasetName ? `Dataset: ${datasetName}` : 'Image Preview'}
          </Header>
        }
      >
        <Container>
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}

          {loading ? (
            <Box textAlign="center" padding="xl">
              <Spinner size="large" />
              <Box variant="p" padding={{ top: 's' }}>
                Loading images...
              </Box>
            </Box>
          ) : (
            <SpaceBetween size="m">
              {totalFound > 0 && (
                <Box>
                  <Badge color="blue">
                    Showing {images.length} of {totalFound} images
                  </Badge>
                  {totalFound > images.length && (
                    <Box variant="small" color="text-body-secondary" padding={{ top: 'xs' }}>
                      Only showing first {images.length} images for performance
                    </Box>
                  )}
                </Box>
              )}

              {images.length === 0 && !loading && (
                <Alert type="info">
                  No images found in this dataset prefix.
                </Alert>
              )}

              {images.length > 0 && (
                <Grid
                  gridDefinition={[
                    { colspan: { default: 12, xs: 6, s: 4, m: 3, l: 2 } },
                    { colspan: { default: 12, xs: 6, s: 4, m: 3, l: 2 } },
                    { colspan: { default: 12, xs: 6, s: 4, m: 3, l: 2 } },
                    { colspan: { default: 12, xs: 6, s: 4, m: 3, l: 2 } },
                    { colspan: { default: 12, xs: 6, s: 4, m: 3, l: 2 } },
                    { colspan: { default: 12, xs: 6, s: 4, m: 3, l: 2 } },
                  ]}
                >
                  {images.map((image) => (
                    <div
                      key={image.key}
                      style={{
                        border: '1px solid #e9ebed',
                        borderRadius: '8px',
                        cursor: 'pointer',
                        transition: 'all 0.2s ease',
                        padding: '12px',
                      }}
                      onClick={() => setSelectedImage(image)}
                    >
                      <Box>
                      <SpaceBetween size="xs">
                        <Box textAlign="center">
                          <img
                            src={image.presigned_url}
                            alt={image.filename}
                            style={{
                              maxWidth: '100%',
                              maxHeight: '150px',
                              objectFit: 'contain',
                              borderRadius: '4px',
                            }}
                            onError={(e) => {
                              const target = e.target as HTMLImageElement;
                              target.style.display = 'none';
                              target.nextElementSibling!.textContent = 'Failed to load image';
                            }}
                          />
                          <div
                            style={{ display: 'none' }}
                          />
                        </Box>
                        <Box>
                          <div
                            style={{
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                              fontSize: '12px',
                              fontWeight: 'bold',
                            }}
                            title={image.filename}
                          >
                            {image.filename}
                          </div>
                          <Box variant="small" color="text-body-secondary">
                            {formatFileSize(image.size)}
                          </Box>
                        </Box>
                      </SpaceBetween>
                      </Box>
                    </div>
                  ))}
                </Grid>
              )}
            </SpaceBetween>
          )}
        </Container>
      </Modal>

      {/* Full-size image modal */}
      {selectedImage && (
        <Modal
          onDismiss={() => setSelectedImage(null)}
          visible={!!selectedImage}
          size="large"
          header={
            <Header
              variant="h3"
              description={`${formatFileSize(selectedImage.size)} • ${new Date(
                selectedImage.last_modified
              ).toLocaleString()}`}
              actions={
                <Button onClick={() => setSelectedImage(null)}>Close</Button>
              }
            >
              {selectedImage.filename}
            </Header>
          }
        >
          <Box textAlign="center" padding="m">
            <img
              src={selectedImage.presigned_url}
              alt={selectedImage.filename}
              style={{
                maxWidth: '100%',
                maxHeight: '70vh',
                objectFit: 'contain',
                borderRadius: '8px',
                boxShadow: '0 4px 12px rgba(0, 0, 0, 0.1)',
              }}
            />
          </Box>
        </Modal>
      )}
    </>
  );
}