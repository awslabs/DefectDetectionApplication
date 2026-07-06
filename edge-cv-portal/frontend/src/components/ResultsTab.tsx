import { useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import ResultsViewer, { type Capture } from './ResultsViewer';

interface Props {
  deviceId: string;
  usecaseId: string;
}

/**
 * Results tab (Requirement 4).
 *
 * Reachable from the device detail navigation. Fetches inference-results
 * captures for the device from the portal-backend captures endpoint
 * (`apiService.getCaptures`, presigned URLs + parsed Detections_Block) and
 * renders the selected capture in the prop-driven `ResultsViewer`, which draws
 * detection boxes / labels for detection captures, a "No objects detected"
 * indicator for zero-object captures, and the mask overlay for anomaly
 * captures.
 *
 * The captures endpoint requires an S3 `prefix` (the capture folder in the
 * inference-results bucket). It defaults to the device id and can be edited so
 * an operator can point at a specific capture folder; `device_id` is also sent
 * so the backend can scope the search prefix.
 */
export default function ResultsTab({ deviceId, usecaseId }: Props) {
  const [prefix, setPrefix] = useState(deviceId);
  const [captures, setCaptures] = useState<Capture[]>([]);
  const [selected, setSelected] = useState<Capture | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const loadCaptures = async () => {
    if (!usecaseId || !prefix) {
      setError('A capture prefix is required');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await apiService.getCaptures({
        usecase_id: usecaseId,
        prefix,
        device_id: deviceId,
        limit: 50,
      });
      setCaptures(response.captures);
      setSelected(response.captures[0] ?? null);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load captures');
    } finally {
      setLoading(false);
    }
  };

  const renderResultBadge = (capture: Capture) => {
    switch (capture.inference_result_type) {
      case 'Detection':
        return <Badge color="blue">Detection</Badge>;
      case 'Anomaly':
        return <Badge color="red">Anomaly</Badge>;
      case 'Normal':
        return <Badge color="green">Normal</Badge>;
      default:
        return <Badge color="grey">Unknown</Badge>;
    }
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description="Browse inference-results captures uploaded from this device and inspect detection, zero-object, and anomaly results."
          >
            Inference Results
          </Header>
        }
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={2}>
            <FormField
              label="Capture prefix"
              description="S3 prefix (capture folder) in the inference-results bucket."
            >
              <Input
                value={prefix}
                onChange={({ detail }) => setPrefix(detail.value)}
                placeholder="e.g. my-device or my-device/2024-06-01"
                disabled={loading}
              />
            </FormField>
            <Box>
              <Box variant="awsui-key-label">&nbsp;</Box>
              <Button
                variant="primary"
                iconName="search"
                onClick={loadCaptures}
                loading={loading}
                disabled={!prefix}
              >
                Load captures
              </Button>
            </Box>
          </ColumnLayout>
        </SpaceBetween>
      </Container>

      {loading && captures.length === 0 ? (
        <Container>
          <Box textAlign="center" padding="xxl">
            <Spinner size="large" />
            <Box variant="p" color="text-body-secondary" margin={{ top: 's' }}>
              Loading captures...
            </Box>
          </Box>
        </Container>
      ) : loaded && captures.length === 0 ? (
        <Container>
          <Box textAlign="center" padding="xxl" color="text-body-secondary">
            No captures found under this prefix.
          </Box>
        </Container>
      ) : captures.length > 0 ? (
        <ColumnLayout columns={2}>
          <Container header={<Header variant="h3" counter={`(${captures.length})`}>Captures</Header>}>
            <Table
              variant="embedded"
              selectionType="single"
              selectedItems={selected ? [selected] : []}
              onSelectionChange={({ detail }) =>
                setSelected(detail.selectedItems[0] ?? null)
              }
              trackBy="capture_id"
              items={captures}
              columnDefinitions={[
                {
                  id: 'capture_id',
                  header: 'Capture',
                  cell: (item: Capture) => item.capture_id,
                },
                {
                  id: 'result',
                  header: 'Result',
                  cell: (item: Capture) => renderResultBadge(item),
                },
                {
                  id: 'objects',
                  header: 'Objects',
                  cell: (item: Capture) =>
                    item.inference_result_type === 'Detection'
                      ? item.detection_count
                      : '-',
                },
              ]}
              empty={
                <Box textAlign="center" color="inherit">
                  No captures
                </Box>
              }
            />
          </Container>
          <ResultsViewer capture={selected} />
        </ColumnLayout>
      ) : (
        <Container>
          <Box textAlign="center" padding="l" color="text-body-secondary">
            <StatusIndicator type="info">
              Enter a capture prefix and load captures to view results.
            </StatusIndicator>
          </Box>
        </Container>
      )}
    </SpaceBetween>
  );
}
