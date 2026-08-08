/**
 * Render tests for the Build_Job detail page
 * (portal-build-fleet-and-workflow-gates Requirement 4.3): the detail
 * view shows the Build_Target, execution mode, requesting user, and
 * submission time; the assigned Build_Server once assigned; the start
 * time once the job entered building; the end time once terminal; and
 * the published artifact identifiers for a succeeded job.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import BuildDetail from './BuildDetail';
import type { BuildJob } from './types';

const { getBuild, getBuildLogs, cancelBuild, retryBuild, navigateMock } = vi.hoisted(() => ({
  getBuild: vi.fn(),
  getBuildLogs: vi.fn(),
  cancelBuild: vi.fn(),
  retryBuild: vi.fn(),
  navigateMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { getBuild, getBuildLogs, cancelBuild, retryBuild },
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ buildJobId: 'job-1' }),
}));

const CREATED_AT = new Date('2024-05-01T10:00:00Z').getTime();
const STARTED_AT = new Date('2024-05-01T10:05:00Z').getTime();
const ENDED_AT = new Date('2024-05-01T11:30:00Z').getTime();

const SUCCEEDED_JOB: BuildJob = {
  build_job_id: 'job-1',
  request_id: 'r-1',
  request_order: 0,
  predecessor_job_id: null,
  build_target: 'JP6',
  component_name: 'aws.edgeml.dda.LocalServer.arm64JP6',
  required_arch: 'arm64',
  execution_mode: 'dedicated',
  server_id: 's-1',
  status: 'succeeded',
  requested_by: 'alice',
  created_at: CREATED_AT,
  started_at: STARTED_AT,
  ended_at: ENDED_AT,
  result: {
    component_name: 'aws.edgeml.dda.LocalServer.arm64JP6',
    published_version: '2.4.1',
    pushed_image_refs: ['123.dkr.ecr.us-east-1.amazonaws.com/flask-app:jp6-2.4.1'],
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  getBuild.mockResolvedValue({ job: SUCCEEDED_JOB });
  getBuildLogs.mockResolvedValue({ events: [], nextToken: null });
});

describe('BuildDetail job fields (Requirement 4.3)', () => {
  it('shows target, mode, requester, server, times, and published artifacts for a succeeded job', async () => {
    render(<BuildDetail />);
    await screen.findByText('Job information');
    expect(getBuild).toHaveBeenCalledWith('job-1');

    // Target, execution mode (with the assigned Build_Server), and
    // requesting user from job creation (Req 4.3).
    expect(screen.getByText('JP6')).toBeInTheDocument();
    expect(screen.getByText('dedicated (s-1)')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.getByText('job-1')).toBeInTheDocument();

    // Submission, start, and end times (Req 4.3). The page formats
    // epoch ms with toLocaleString; assert against the same rendering.
    expect(screen.getByText(new Date(CREATED_AT).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText(new Date(STARTED_AT).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText(new Date(ENDED_AT).toLocaleString())).toBeInTheDocument();

    // Terminal status is visible (header badge + status field).
    expect(screen.getAllByText('succeeded').length).toBeGreaterThan(0);

    // Published artifact identifiers of the succeeded job (Req 4.3, 4.7).
    expect(screen.getByText('Published artifacts')).toBeInTheDocument();
    expect(screen.getByText('2.4.1')).toBeInTheDocument();
    expect(
      screen.getByText('123.dkr.ecr.us-east-1.amazonaws.com/flask-app:jp6-2.4.1'),
    ).toBeInTheDocument();
  });

  it('shows "-" for the start and end times a queued job has not reached', async () => {
    getBuild.mockResolvedValue({
      job: {
        ...SUCCEEDED_JOB,
        execution_mode: 'ephemeral',
        server_id: null,
        status: 'queued',
        started_at: undefined,
        ended_at: undefined,
        result: undefined,
      },
    });
    render(<BuildDetail />);
    await screen.findByText('Job information');

    // Fields present from creation (Req 4.3).
    expect(screen.getByText('JP6')).toBeInTheDocument();
    expect(screen.getByText('ephemeral')).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.getByText(new Date(CREATED_AT).toLocaleString())).toBeInTheDocument();

    // Start/end not reached yet render the "-" placeholder, and no
    // published artifacts section exists for a non-succeeded job.
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('Published artifacts')).toBeNull();
  });
});

describe('BuildDetail execution diagnostics (build-fleet-execution-failures Req 2.3, 2.18, 3.10)', () => {
  const FAILED_JOB: BuildJob = {
    ...SUCCEEDED_JOB,
    build_target: 'AMD64',
    required_arch: 'x86_64',
    status: 'failed',
    result: undefined,
    error: {
      kind: 'build',
      code: 'COMMAND_EXECUTION_FAILED',
      message: 'The build agent SSM command ended with status Failed.',
    },
  };

  it('shows retained SSM and timeout evidence when CloudWatch events are absent', async () => {
    getBuild.mockResolvedValue({ job: FAILED_JOB });
    getBuildLogs.mockResolvedValue({
      events: [],
      nextToken: null,
      diagnostic: {
        schemaVersion: 1,
        classification: 'COMMAND_EXECUTION_FAILED',
        status: 'Failed',
        statusDetails: 'Failed',
        responseCode: 127,
        stdout: { available: true, text: '', truncated: false },
        stderr: {
          available: true,
          text: 'bash: portal-build-agent.sh: No such file or directory',
          truncated: true,
        },
        timing: { queueMs: 1_000, provisioningMs: null, executionMs: 8_000 },
        timeout: {
          kind: 'MAX_RUNTIME_EXCEEDED',
          budgetMs: 4 * 60 * 60 * 1000,
          budgetSource: 'max_runtime_hours',
          lastHeartbeatAt: STARTED_AT,
          lastProgressAt: null,
        },
        observedAt: ENDED_AT,
        complete: true,
      },
    });
    render(<BuildDetail />);
    await screen.findByText('Execution diagnostics');

    // Safe classification, status details, and response code (Req 2.3).
    expect(screen.getByText('COMMAND_EXECUTION_FAILED')).toBeInTheDocument();
    expect(screen.getByText('Response code')).toBeInTheDocument();
    expect(screen.getByText('127')).toBeInTheDocument();

    // Phase durations, separately accounted (Req 2.18); a phase with
    // no recorded evidence states so explicitly rather than fabricating.
    expect(screen.getByText('Queue wait')).toBeInTheDocument();
    expect(screen.getByText('1s')).toBeInTheDocument();
    expect(screen.getByText('Execution')).toBeInTheDocument();
    expect(screen.getByText('8s')).toBeInTheDocument();
    expect(screen.getByText('Provisioning')).toBeInTheDocument();

    // Timeout kind/budget/source and last heartbeat/progress (Req 2.18).
    expect(
      screen.getByText('MAX_RUNTIME_EXCEEDED (budget 4h 0s from max_runtime_hours)'),
    ).toBeInTheDocument();
    expect(screen.getByText('Last heartbeat')).toBeInTheDocument();
    expect(screen.getByText('Last progress')).toBeInTheDocument();

    // stderr excerpt with its explicit truncation state; stdout is
    // available-but-empty and states so (Req 2.2, 2.18).
    expect(
      screen.getByText('bash: portal-build-agent.sh: No such file or directory'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Excerpt truncated to the retained byte limit.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Available but empty/),
    ).toBeInTheDocument();

    // The empty-log message is never the only explanation (Req 3.10):
    // it points at the retained diagnostics instead of the legacy text.
    expect(
      screen.queryByDisplayValue(
        'No log output was recorded for this build job.',
      ),
    ).toBeNull();
    expect(
      screen.getByDisplayValue(/Execution diagnostics section below/),
    ).toBeInTheDocument();
  });

  it('states that evidence is unavailable or expired when neither source exists', async () => {
    getBuild.mockResolvedValue({ job: FAILED_JOB });
    getBuildLogs.mockResolvedValue({ events: [], nextToken: null });
    render(<BuildDetail />);
    await screen.findByText('Job information');

    // No diagnostics panel is fabricated (Req 3.10)...
    expect(screen.queryByText('Execution diagnostics')).toBeNull();
    // ...and the log area states the evidence is unavailable/expired.
    expect(
      screen.getByDisplayValue(/evidence is unavailable or may have expired/),
    ).toBeInTheDocument();
  });

  it('keeps the legacy succeeded rendering unchanged when the diagnostic is omitted', async () => {
    render(<BuildDetail />);
    await screen.findByText('Job information');

    // Legacy response (no diagnostic field): no diagnostics panel and
    // the succeeded view renders exactly as before (Req 3.6, 3.10).
    expect(screen.queryByText('Execution diagnostics')).toBeNull();
    expect(screen.getByText('Published artifacts')).toBeInTheDocument();
  });
});

describe('BuildDetail diagnostics availability, redaction, and unchanged views (build-fleet-execution-failures task 10.7, Req 2.2, 2.18, 3.6, 3.10)', () => {
  // Property 13 rendering counterpart:
  // **Validates: Requirements 2.2, 2.3, 2.18, 3.6**
  const FAILED_JOB: BuildJob = {
    ...SUCCEEDED_JOB,
    build_target: 'AMD64',
    required_arch: 'x86_64',
    status: 'failed',
    result: undefined,
    error: {
      kind: 'build',
      code: 'AGENT_RESULT_MISSING',
      message: 'The build agent reported no terminal result.',
    },
  };

  it('marks unavailable stdout and stderr explicitly instead of fabricating output', async () => {
    getBuild.mockResolvedValue({ job: FAILED_JOB });
    getBuildLogs.mockResolvedValue({
      events: [],
      nextToken: null,
      diagnostic: {
        schemaVersion: 1,
        classification: 'AGENT_RESULT_MISSING',
        status: 'Success',
        statusDetails: null,
        responseCode: 0,
        stdout: { available: false },
        stderr: { available: false },
        timing: { queueMs: null, provisioningMs: null, executionMs: null },
        observedAt: ENDED_AT,
        complete: false,
      },
    });
    render(<BuildDetail />);
    await screen.findByText('Execution diagnostics');

    // Both streams state the explicit unavailable/expired condition
    // (Req 2.18); no excerpt text is fabricated.
    expect(
      screen.getAllByText('Not available from the command provider.'),
    ).toHaveLength(2);
    expect(screen.getByText('AGENT_RESULT_MISSING')).toBeInTheDocument();
    // Phases without recorded evidence say so explicitly.
    expect(screen.getAllByText('not recorded').length).toBeGreaterThanOrEqual(3);
  });

  it('renders persisted redacted text intact and never a raw secret', async () => {
    // The API returns persisted, ALREADY-REDACTED text (Req 2.10); the
    // page renders it verbatim and no raw secret shape can appear.
    const RAW_SECRET_CANARY = 'AKIA' + 'CANARYBUILDDETAIL';
    getBuild.mockResolvedValue({ job: FAILED_JOB });
    getBuildLogs.mockResolvedValue({
      events: [],
      nextToken: null,
      diagnostic: {
        schemaVersion: 1,
        classification: 'COMMAND_EXECUTION_FAILED',
        status: 'Failed',
        statusDetails: 'authorization: [REDACTED] rejected',
        responseCode: 1,
        stdout: { available: true, text: '', truncated: false },
        stderr: {
          available: true,
          text: 'aws_secret_access_key=[REDACTED] push failed',
          truncated: false,
        },
        timing: { queueMs: 0, provisioningMs: null, executionMs: 2_000 },
        observedAt: ENDED_AT,
        complete: true,
      },
    });
    const { container } = render(<BuildDetail />);
    await screen.findByText('Execution diagnostics');

    // The [REDACTED] markers survive verbatim into the render...
    expect(
      screen.getByText('aws_secret_access_key=[REDACTED] push failed'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('authorization: [REDACTED] rejected'),
    ).toBeInTheDocument();
    // ...and nothing anywhere in the page carries a raw secret shape.
    expect(container.textContent).not.toContain(RAW_SECRET_CANARY);
    expect((container.textContent || '').replace(/\[REDACTED\]/g, '')).not.toMatch(
      /AKIA[0-9A-Z]{16}/,
    );
  });

  it('keeps the running view unchanged when no diagnostic is returned', async () => {
    getBuild.mockResolvedValue({
      job: { ...SUCCEEDED_JOB, status: 'building', ended_at: undefined, result: undefined },
    });
    getBuildLogs.mockResolvedValue({
      events: [{ timestamp: STARTED_AT, message: 'step 1 ok' }],
      nextToken: null,
    });
    render(<BuildDetail />);
    await screen.findByText('Job information');

    // The running view renders exactly as before (Req 3.6, 3.10): the
    // polling notice, the streamed log line, no diagnostics panel.
    expect(
      screen.getByText(/The log refreshes every 30 seconds while the build runs/),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue(/step 1 ok/)).toBeInTheDocument();
    expect(screen.queryByText('Execution diagnostics')).toBeNull();
    expect(screen.getByRole('button', { name: 'Cancel build' })).toBeInTheDocument();
  });
});

describe('BuildDetail built source (build-source-selection Requirement 2.6)', () => {
  it('shows the repository, ref, and resolved commit the job was built from', async () => {
    getBuild.mockResolvedValue({
      job: {
        ...SUCCEEDED_JOB,
        config_snapshot: {
          repository: 'https://github.com/alice/DefectDetectionApplication',
          source_ref: 'feature/portal-build-fleet-and-workflow-gates',
        },
        source_commit: '479ab7f0123456789abcdef0123456789abcdef0',
      },
    });
    render(<BuildDetail />);
    await screen.findByText('Job information');

    expect(screen.getByText('Repository')).toBeInTheDocument();
    expect(
      screen.getByText('https://github.com/alice/DefectDetectionApplication'),
    ).toBeInTheDocument();
    expect(screen.getByText('Source ref')).toBeInTheDocument();
    expect(
      screen.getByText('feature/portal-build-fleet-and-workflow-gates'),
    ).toBeInTheDocument();
    expect(screen.getByText('Resolved commit')).toBeInTheDocument();
    expect(
      screen.getByText('479ab7f0123456789abcdef0123456789abcdef0'),
    ).toBeInTheDocument();
  });

  it('shows "default branch" for a null source_ref on a job whose snapshot has a repository', async () => {
    getBuild.mockResolvedValue({
      job: {
        ...SUCCEEDED_JOB,
        config_snapshot: {
          repository: 'https://github.com/awslabs/DefectDetectionApplication',
          source_ref: null,
        },
        source_commit: undefined,
      },
    });
    render(<BuildDetail />);
    await screen.findByText('Job information');

    expect(
      screen.getByText('https://github.com/awslabs/DefectDetectionApplication'),
    ).toBeInTheDocument();
    // A null ref on a source-selection-era job means the repository's
    // default branch; the unrecorded commit shows the placeholder.
    expect(screen.getByText('default branch')).toBeInTheDocument();
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(1);
  });

  it('shows the placeholder on all three source rows for a legacy job without snapshot source fields', async () => {
    getBuild.mockResolvedValue({
      job: { ...SUCCEEDED_JOB, config_snapshot: {}, source_commit: undefined },
    });
    render(<BuildDetail />);
    await screen.findByText('Job information');

    // The rows exist and each renders the '-' placeholder (Repository,
    // Source ref, Resolved commit).
    expect(screen.getByText('Repository')).toBeInTheDocument();
    expect(screen.getByText('Source ref')).toBeInTheDocument();
    expect(screen.getByText('Resolved commit')).toBeInTheDocument();
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(3);
  });
});
