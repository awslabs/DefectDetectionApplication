import { useState } from 'react';
import {
  SpaceBetween,
  Button,
  Alert,
  Container,
  Header,
  Box,
  Badge,
  Tabs,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface LogAnalysis {
  device_id: string;
  analysis_timestamp: string;
  issues_detected: number;
  critical_count: number;
  high_count: number;
  medium_count: number;
  low_count: number;
  issues: Array<{
    issue_id: string;
    title: string;
    severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';
    likely_causes: string[];
    recommended_actions: string[];
    prevention_tips: string[];
  }>;
  next_steps: string[];
}

interface LogsDiagnosticsTabProps {
  deviceId: string;
  usecaseId: string;
}

export default function LogsDiagnosticsTab({ deviceId, usecaseId }: LogsDiagnosticsTabProps) {
  const [analysis, setAnalysis] = useState<LogAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hoursBack, setHoursBack] = useState(1);
  const [activeTabId, setActiveTabId] = useState('summary');

  const handleAnalyzeLogs = async () => {
    setAnalyzing(true);
    setError(null);
    try {
      const response = await apiService.analyzeLogs(deviceId, usecaseId, { hours: hoursBack });
      setAnalysis(response.analysis);
    } catch (err: any) {
      setError(err.message || 'Failed to analyze logs');
      console.error('Failed to analyze logs:', err);
    } finally {
      setAnalyzing(false);
    }
  };

  const getSeverityIcon = (severity: string) => {
    switch (severity) {
      case 'CRITICAL':
        return '🚨';
      case 'HIGH':
        return '⚠️';
      case 'MEDIUM':
        return 'ℹ️';
      case 'LOW':
        return '✓';
      default:
        return '•';
    }
  };

  const getStatusIndicator = (severity: string) => {
    switch (severity) {
      case 'CRITICAL':
        return <StatusIndicator type="error">{severity}</StatusIndicator>;
      case 'HIGH':
        return <StatusIndicator type="warning">{severity}</StatusIndicator>;
      case 'MEDIUM':
        return <StatusIndicator type="info">{severity}</StatusIndicator>;
      case 'LOW':
        return <StatusIndicator type="success">{severity}</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{severity}</StatusIndicator>;
    }
  };

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Analysis Controls */}
      <Container header={<Header>Log Analysis</Header>}>
        <SpaceBetween size="m">
          <Box>
            <Box variant="awsui-key-label">Time Range</Box>
            <select
              value={hoursBack}
              onChange={(e) => setHoursBack(parseInt(e.target.value))}
              style={{
                padding: '8px 12px',
                borderRadius: '4px',
                border: '1px solid #aab7b8',
                fontSize: '14px',
              }}
            >
              <option value={1}>Last 1 hour</option>
              <option value={6}>Last 6 hours</option>
              <option value={24}>Last 24 hours</option>
              <option value={7}>Last 7 days</option>
            </select>
          </Box>
          <Button
            variant="primary"
            onClick={handleAnalyzeLogs}
            loading={analyzing}
            disabled={analyzing}
          >
            {analyzing ? 'Analyzing Logs...' : 'Analyze Logs with AI'}
          </Button>
        </SpaceBetween>
      </Container>

      {/* Analysis Results */}
      {analysis && (
        <SpaceBetween size="l">
          {/* Summary Stats */}
          <Container header={<Header>Analysis Summary</Header>}>
            <SpaceBetween size="m">
              <Box>
                <Box variant="awsui-key-label">Analysis Time</Box>
                <Box>{new Date(analysis.analysis_timestamp).toLocaleString()}</Box>
              </Box>

              {/* Issue Counts */}
              <Box>
                <Box variant="awsui-key-label">Issues Detected</Box>
                <SpaceBetween direction="horizontal" size="m">
                  {analysis.critical_count > 0 && (
                    <Box>
                      <Badge color="red">{analysis.critical_count} Critical</Badge>
                    </Box>
                  )}
                  {analysis.high_count > 0 && (
                    <Box>
                      <Badge color="red">{analysis.high_count} High</Badge>
                    </Box>
                  )}
                  {analysis.medium_count > 0 && (
                    <Box>
                      <Badge color="blue">{analysis.medium_count} Medium</Badge>
                    </Box>
                  )}
                  {analysis.low_count > 0 && (
                    <Box>
                      <Badge color="green">{analysis.low_count} Low</Badge>
                    </Box>
                  )}
                  {analysis.issues_detected === 0 && (
                    <Box>
                      <Badge color="green">✓ No Issues</Badge>
                    </Box>
                  )}
                </SpaceBetween>
              </Box>

              {/* Next Steps */}
              {analysis.next_steps.length > 0 && (
                <Box>
                  <Box variant="awsui-key-label">Next Steps</Box>
                  <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
                    {analysis.next_steps.map((step, idx) => (
                      <li key={idx} style={{ marginBottom: '4px' }}>
                        {step}
                      </li>
                    ))}
                  </ul>
                </Box>
              )}
            </SpaceBetween>
          </Container>

          {/* Detailed Issues */}
          {analysis.issues_detected > 0 && (
            <Container header={<Header>Detected Issues</Header>}>
              <Tabs
                activeTabId={activeTabId}
                onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
                tabs={analysis.issues.map((issue) => ({
                  id: issue.issue_id,
                  label: (
                    <SpaceBetween direction="horizontal" size="xs">
                      <span>{getSeverityIcon(issue.severity)}</span>
                      <span>{issue.title}</span>
                    </SpaceBetween>
                  ),
                  content: (
                    <SpaceBetween size="l">
                      {/* Severity */}
                      <Box>
                        <Box variant="awsui-key-label">Severity</Box>
                        {getStatusIndicator(issue.severity)}
                      </Box>

                      {/* Likely Causes */}
                      <Box>
                        <Box variant="awsui-key-label">Likely Causes</Box>
                        <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
                          {issue.likely_causes.map((cause, causeIdx) => (
                            <li key={causeIdx} style={{ marginBottom: '4px' }}>
                              {cause}
                            </li>
                          ))}
                        </ul>
                      </Box>

                      {/* Recommended Actions */}
                      <Box>
                        <Box variant="awsui-key-label">Recommended Actions</Box>
                        <ol style={{ margin: '8px 0', paddingLeft: '20px' }}>
                          {issue.recommended_actions.map((action, actionIdx) => (
                            <li key={actionIdx} style={{ marginBottom: '8px' }}>
                              <Box>{action}</Box>
                            </li>
                          ))}
                        </ol>
                      </Box>

                      {/* Prevention Tips */}
                      <Box>
                        <Box variant="awsui-key-label">Prevention Tips</Box>
                        <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
                          {issue.prevention_tips.map((tip, tipIdx) => (
                            <li key={tipIdx} style={{ marginBottom: '4px', color: '#0972d3' }}>
                              💡 {tip}
                            </li>
                          ))}
                        </ul>
                      </Box>
                    </SpaceBetween>
                  ),
                }))}
              />
            </Container>
          )}

          {/* No Issues Found */}
          {analysis.issues_detected === 0 && (
            <Alert type="success" header="Device Healthy">
              No issues detected in the logs. The device appears to be operating normally.
            </Alert>
          )}
        </SpaceBetween>
      )}

      {/* Initial State */}
      {!analysis && !analyzing && (
        <Alert type="info">
          Click "Analyze Logs with AI" to scan device logs and get intelligent troubleshooting
          guidance. The analyzer will detect common issues and provide actionable next steps.
        </Alert>
      )}
    </SpaceBetween>
  );
}
