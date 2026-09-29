/**
 * The device page's stream camera support panel (rtsp-rtmp-stream-cameras
 * task 11.6 — Requirement 16.5).
 *
 * Shows the Device_Stream_Capabilities the device's LocalServer probed
 * and reported through the camera registry sync: which stream protocols
 * it can pull, the decoder element it found per codec and decoding path,
 * and the library versions. camera_sync.py stores a sanitized projection
 * as `stream_capabilities` on the registry META item, and
 * `GET /devices/{id}/cameras` returns it only once the device reported it,
 * so the page of a device whose LocalServer predates stream support shows
 * no panel at all.
 */
import {
  Box,
  ColumnLayout,
  Container,
  Header,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import type { DeviceStreamCapabilities } from '../pages/workflows/cameraReference';
import { streamCodecLabel } from '../pages/workflows/cameraReference';

/** One codec the device can decode, with the element per decoding path. */
export interface StreamCodecRow {
  /** The reported codec name, e.g. `h265`. */
  name: string;
  /** Its display label, e.g. `H.265`. */
  label: string;
  /** The hardware decoder element, or null when that path is unavailable. */
  hardware: string | null;
  /** The software decoder element, or null when that path is unavailable. */
  software: string | null;
}

/** The panel's view of a Device_Stream_Capabilities report. */
export interface StreamCapabilitiesSummary {
  /** Each protocol flag: true, false, or null when not reported. */
  protocols: { label: string; supported: boolean | null }[];
  /** The decodable codecs, sorted by name. */
  codecs: StreamCodecRow[];
  versions: { label: string; value: string | null }[];
  /** When the device ran the probe, or null when not reported. */
  probedAtMs: number | null;
}

const PROTOCOL_FLAGS: { key: 'rtsp' | 'rtmp' | 'tls'; label: string }[] = [
  { key: 'rtsp', label: 'RTSP' },
  { key: 'rtmp', label: 'RTMP' },
  { key: 'tls', label: 'TLS (rtsps and rtmps)' },
];

const VERSION_KEYS: { key: 'gstreamer' | 'pyav' | 'ffmpeg'; label: string }[] = [
  { key: 'gstreamer', label: 'GStreamer' },
  { key: 'pyav', label: 'PyAV' },
  { key: 'ffmpeg', label: 'FFmpeg' },
];

function nonEmptyText(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Whether an API value is a Device_Stream_Capabilities object at all. The
 * route omits the key until the device reports one, so anything else
 * (absent, null, or malformed) means "nothing to show".
 */
export function isStreamCapabilities(value: unknown): value is DeviceStreamCapabilities {
  return isRecord(value);
}

/**
 * The panel's view of a report. The backend already sanitizes it, but the
 * view is still total over malformed input: a wrongly typed value reads
 * as "not reported" rather than throwing or rendering an object.
 */
export function summarizeStreamCapabilities(
  capabilities: DeviceStreamCapabilities
): StreamCapabilitiesSummary {
  const raw = capabilities as Record<string, unknown>;
  const codecs = isRecord(raw.codecs) ? raw.codecs : {};
  const probed = raw.probedAtMs;
  return {
    protocols: PROTOCOL_FLAGS.map(({ key, label }) => ({
      label,
      supported: typeof raw[key] === 'boolean' ? (raw[key] as boolean) : null,
    })),
    codecs: Object.keys(codecs)
      .sort()
      .map((name) => {
        const decoders = isRecord(codecs[name]) ? (codecs[name] as Record<string, unknown>) : {};
        return {
          name,
          label: streamCodecLabel(name),
          hardware: nonEmptyText(decoders.hardware),
          software: nonEmptyText(decoders.software),
        };
      }),
    versions: VERSION_KEYS.map(({ key, label }) => ({ label, value: nonEmptyText(raw[key]) })),
    probedAtMs:
      typeof probed === 'number' && Number.isFinite(probed) && probed > 0 ? probed : null,
  };
}

function ProtocolIndicator({ supported }: { supported: boolean | null }) {
  if (supported === true) {
    return <StatusIndicator type="success">Supported</StatusIndicator>;
  }
  if (supported === false) {
    return <StatusIndicator type="stopped">Not supported</StatusIndicator>;
  }
  return <Box color="text-status-inactive">Not reported</Box>;
}

function DecoderCell({ element }: { element: string | null }) {
  return element !== null ? (
    <Box variant="code">{element}</Box>
  ) : (
    <Box color="text-status-inactive">Not available</Box>
  );
}

export default function DeviceStreamCapabilitiesPanel({
  capabilities,
}: {
  capabilities: DeviceStreamCapabilities;
}) {
  const summary = summarizeStreamCapabilities(capabilities);
  return (
    <Container
      data-testid="device-stream-capabilities"
      header={
        <Header
          variant="h2"
          description={
            summary.probedAtMs !== null
              ? `Probed by the device's LocalServer at ${new Date(summary.probedAtMs).toLocaleString()}`
              : "Reported by the device's LocalServer"
          }
        >
          Stream camera support
        </Header>
      }
    >
      <SpaceBetween size="l">
        <ColumnLayout columns={2} variant="text-grid">
          <KeyValuePairs
            columns={1}
            items={summary.protocols.map((protocol) => ({
              label: protocol.label,
              value: <ProtocolIndicator supported={protocol.supported} />,
            }))}
          />
          <KeyValuePairs
            columns={1}
            items={summary.versions.map((version) => ({
              label: version.label,
              value: version.value ?? 'Not reported',
            }))}
          />
        </ColumnLayout>
        <Table
          variant="embedded"
          resizableColumns
          header={
            <Header
              variant="h3"
              description="The decoder the device uses for each codec. With the default decoder policy it prefers hardware and falls back to software."
            >
              Decodable codecs
            </Header>
          }
          items={summary.codecs}
          trackBy="name"
          columnDefinitions={[
            { id: 'codec', header: 'Codec', cell: (row: StreamCodecRow) => row.label },
            {
              id: 'hardware',
              header: 'Hardware decoder',
              cell: (row: StreamCodecRow) => <DecoderCell element={row.hardware} />,
            },
            {
              id: 'software',
              header: 'Software decoder',
              cell: (row: StreamCodecRow) => <DecoderCell element={row.software} />,
            },
          ]}
          empty={
            <Box textAlign="center" color="inherit">
              The device reported no codec it can decode, so stream cameras cannot run on it.
            </Box>
          }
        />
      </SpaceBetween>
    </Container>
  );
}
