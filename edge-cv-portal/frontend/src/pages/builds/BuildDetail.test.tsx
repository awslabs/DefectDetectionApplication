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
