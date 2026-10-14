import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Alert,
  Button,
  Toggle,
  FormField,
  Input,
  StatusIndicator,
  ColumnLayout,
  Popover,
  Tabs,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface Props {
  deviceId: string;
  usecaseId: string;
}

/**
 * Remote Access (SSH) via AWS IoT Secure Tunneling.
 *
 * Edge devices are behind NAT with no inbound port, so access is over an
 * outbound tunnel to AWS IoT — there is no security group / IP allowlist on the
 * tunnel path. Access is gated by IAM (who can open a tunnel) + short-lived
 * tunnel tokens. See docs/connect-to-device.md.
 */
export default function RemoteAccessTab({ deviceId, usecaseId }: Props) {
  const [enabled, setEnabled] = useState(false);
  const [componentVersion, setComponentVersion] = useState<string | null>(null);
  const [maxVersion, setMaxVersion] = useState<string | null>(null);
  const [osUser, setOsUser] = useState('ggc_user');
  const [statusLoading, setStatusLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [tunnel, setTunnel] = useState<{
    token: string;
    tunnelId: string;
    region: string;
    lifetime: number;
  } | null>(null);

  const loadStatus = async () => {
    setStatusLoading(true);
    setError(null);
    try {
      const s = await apiService.getSshTunnelStatus(deviceId, usecaseId);
      setEnabled(s.enabled);
      setComponentVersion(s.component_version || null);
      setMaxVersion(s.secure_tunneling_max_version || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load tunnel status');
    } finally {
      setStatusLoading(false);
    }
  };

  useEffect(() => {
    if (deviceId && usecaseId) loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId, usecaseId]);

  const handleToggle = async (next: boolean) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const r = await apiService.setSshTunnel(deviceId, usecaseId, next, osUser);
      setEnabled(next);
      setMessage(r.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update tunnel setting');
    } finally {
      setBusy(false);
    }
  };

  const handleOpen = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    setTunnel(null);
    try {
      const r = await apiService.openSshTunnel(deviceId, usecaseId, 60);
      setTunnel({
        token: r.source_access_token,
        tunnelId: r.tunnel_id,
        region: r.region,
        lifetime: r.lifetime_minutes,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to open tunnel');
    } finally {
      setBusy(false);
    }
  };

  // A preformatted, copyable command block for the connect instructions.
  const renderCommandBlock = (cmd: string) => (
    <SpaceBetween size="xs">
      <pre
        style={{
          background: '#f4f4f4',
          border: '1px solid #d5dbdb',
          borderRadius: 4,
          padding: '12px',
          margin: 0,
          overflowX: 'auto',
          fontFamily: 'Monaco, Menlo, "Courier New", monospace',
          fontSize: 12,
          lineHeight: 1.5,
          whiteSpace: 'pre',
        }}
      >
        {cmd}
      </pre>
      <Popover
        dismissButton={false}
        position="top"
        size="small"
        triggerType="custom"
        content={<StatusIndicator type="success">Copied</StatusIndicator>}
      >
        <Button iconName="copy" onClick={() => navigator.clipboard.writeText(cmd)}>
          Copy commands
        </Button>
      </Popover>
    </SpaceBetween>
  );

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Alert type="info" header="How access is secured">
        This device is reached over <strong>AWS IoT Secure Tunneling</strong> — an
        outbound connection from the device to AWS IoT, with <strong>no inbound
        port</strong> on the device. Because nothing listens for inbound
        connections, a security group / IP allowlist does not apply here. Access
        is controlled by <strong>IAM</strong> (only authorized portal users can
        open a tunnel) and by <strong>short-lived tunnel tokens</strong>. Each
        opened tunnel expires automatically.
      </Alert>

      <Container
        header={
          <Header
            variant="h2"
            description="Deploy the AWS Secure Tunneling component so this device can accept SSH over a tunnel."
          >
            SSH Remote Access
          </Header>
        }
      >
        <SpaceBetween size="m">
          {message && (
            <Alert type="success" dismissible onDismiss={() => setMessage(null)}>
              {message}
            </Alert>
          )}

          <ColumnLayout columns={2} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Status</Box>
              {statusLoading ? (
                <StatusIndicator type="loading">Checking…</StatusIndicator>
              ) : enabled ? (
                <StatusIndicator type="success">
                  Enabled{componentVersion ? ` (v${componentVersion})` : ''}
                </StatusIndicator>
              ) : (
                <StatusIndicator type="stopped">Disabled</StatusIndicator>
              )}
            </div>
            <div>
              <Box variant="awsui-key-label">SSH login user</Box>
              <Input
                value={osUser}
                onChange={({ detail }) => setOsUser(detail.value)}
                disabled={busy}
                placeholder="ggc_user"
              />
              <Box variant="small" color="text-body-secondary">
                e.g. <code>ubuntu</code> on EC2, <code>ggc_user</code> (Greengrass
                default), or your device user.
              </Box>
            </div>
          </ColumnLayout>

          <FormField label="Enable Secure Tunneling on this device">
            <Toggle
              checked={enabled}
              disabled={busy || statusLoading}
              onChange={({ detail }) => handleToggle(detail.checked)}
            >
              {enabled ? 'Enabled — device can accept SSH tunnels' : 'Disabled'}
            </Toggle>
          </FormField>

          <Box variant="p" color="text-body-secondary">
            Enabling deploys <code>aws.greengrass.SecureTunneling</code> to the
            device (merged with its existing components). Allow a minute for the
            device to pull it before opening a session.
          </Box>

          {maxVersion && (
            <Alert type="info" header="Version pinned for this device">
              This device runs JetPack 5 (GLIBC 2.31), which is incompatible with{' '}
              <code>aws.greengrass.SecureTunneling</code> 2.0.0 and newer. The
              portal pins it to <strong>v{maxVersion}</strong> here — the newest
              compatible release — so enabling remote access can't break the
              device with a version it can't run.
            </Alert>
          )}

          <div>
            <Button
              variant="primary"
              onClick={handleOpen}
              loading={busy}
              disabled={!enabled || busy}
            >
              Open SSH session
            </Button>
          </div>
        </SpaceBetween>
      </Container>

      {tunnel && (
        <Container header={<Header variant="h2">Tunnel opened</Header>}>
          <SpaceBetween size="m">
            <Alert type="warning">
              This token is shown once and grants SSH access for {tunnel.lifetime}{' '}
              minutes. Treat it like a credential.
            </Alert>
            <ColumnLayout columns={2} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Tunnel ID</Box>
                <Box>{tunnel.tunnelId}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Region</Box>
                <Box>{tunnel.region}</Box>
              </div>
            </ColumnLayout>
            <div>
              <Box variant="awsui-key-label">Source access token</Box>
              <SpaceBetween direction="horizontal" size="xs">
                <Box fontSize="body-s">
                  <code style={{ wordBreak: 'break-all' }}>{tunnel.token}</code>
                </Box>
                <Popover
                  dismissButton={false}
                  position="top"
                  size="small"
                  triggerType="custom"
                  content={<StatusIndicator type="success">Copied</StatusIndicator>}
                >
                  <Button
                    iconName="copy"
                    onClick={() => navigator.clipboard.writeText(tunnel.token)}
                  >
                    Copy
                  </Button>
                </Popover>
              </SpaceBetween>
            </div>
            <Box variant="awsui-key-label">Connect from your computer</Box>
            <Box variant="small" color="text-body-secondary">
              You need the AWS IoT local proxy and an SSH client (built into
              macOS/Linux, WSL, and Windows 10+). The Docker option below needs no
              install and works the same on Windows, WSL, macOS, and Linux. Keep the
              local-proxy running while connected; SSH in from a second terminal.
              The <code>--destination-client-type V1</code> flag is required
              because the device uses the Greengrass Secure Tunneling (V1) component.
            </Box>
            <Tabs
              tabs={[
                {
                  id: 'docker',
                  label: 'Docker (recommended — all platforms)',
                  content: renderCommandBlock(
                    `# Requires Docker. Uses the AWS-published local proxy image (no build).\n` +
                    `# On Apple Silicon Macs, change amd64-latest to arm64-latest.\n` +
                    `# 1) Start the local proxy (keep this terminal open):\n` +
                    `docker run --rm -it -p 5555:5555 \\\n` +
                    `  -e AWSIOT_TUNNEL_ACCESS_TOKEN="${tunnel.token}" \\\n` +
                    `  public.ecr.aws/aws-iot-securetunneling-localproxy/ubuntu-bin:amd64-latest \\\n` +
                    `  --region ${tunnel.region} -s 5555 -b 0.0.0.0 --destination-client-type V1\n\n` +
                    `# 2) In a SECOND terminal, SSH to the device:\n` +
                    `ssh -p 5555 ${osUser}@localhost`
                  ),
                },
                {
                  id: 'unix',
                  label: 'Native binary — macOS / Linux / WSL',
                  content: renderCommandBlock(
                    `# First install localproxy (build from source or prebuilt binary image):\n` +
                    `#   https://github.com/aws-samples/aws-iot-securetunneling-localproxy\n` +
                    `# 1) Start the local proxy (keep this terminal open):\n` +
                    `export AWSIOT_TUNNEL_ACCESS_TOKEN="${tunnel.token}"\n` +
                    `localproxy -r ${tunnel.region} -s 5555 --destination-client-type V1\n\n` +
                    `# 2) In a SECOND terminal, SSH to the device:\n` +
                    `ssh -p 5555 ${osUser}@localhost`
                  ),
                },
                {
                  id: 'windows',
                  label: 'Native binary — Windows (PowerShell)',
                  content: renderCommandBlock(
                    `# First build localproxy for Windows (see repo windows-localproxy-build.md).\n` +
                    `# 1) Start the local proxy (keep this window open):\n` +
                    `$env:AWSIOT_TUNNEL_ACCESS_TOKEN="${tunnel.token}"\n` +
                    `.\\localproxy.exe -r ${tunnel.region} -s 5555 --destination-client-type V1\n\n` +
                    `# 2) In a SECOND PowerShell window, SSH to the device:\n` +
                    `ssh -p 5555 ${osUser}@localhost`
                  ),
                },
              ]}
            />
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
