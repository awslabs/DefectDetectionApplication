/**
 * Component tests for the PortalAdmin security review queue
 * (custom-node-designer task 12.7, Requirements 10.2, 15.6): the pending
 * record display is complete — provenance, per-arch checksums and
 * signatures, and source inspection (10.2) — with the recorded
 * Plugin_Set_Classification alongside the other provenance details
 * (15.6), and approve/reject actions record the decision.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReviewQueue from './ReviewQueue';

const { useAuthMock, listPendingReviews, getVersion, getVersionSource, reviewVersion } =
  vi.hoisted(() => ({
    useAuthMock: vi.fn(),
    listPendingReviews: vi.fn(),
    getVersion: vi.fn(),
    getVersionSource: vi.fn(),
    reviewVersion: vi.fn(),
  }));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: useAuthMock,
}));

vi.mock('./api', () => ({
  nodeDesignerApi: { listPendingReviews, getVersion, getVersionSource, reviewVersion },
}));

const PENDING_SUMMARY = {
  plugin_id: 'p-1',
  version: 2,
  usecase_id: 'uc-1',
  name: 'edgefilter',
  kind: 'imported',
  deepstream: false,
  lifecycle_state: 'dev',
  review_decision: 'pending',
  classification: 'bad',
  build_status: { x86_64: 'succeeded' },
  updated_at: 1700000000000,
};

const PENDING_DETAIL = {
  plugin: {
    plugin_id: 'p-1',
    version: 2,
    usecase_id: 'uc-1',
    name: 'edgefilter',
    description: '',
    kind: 'imported',
    deepstream: false,
    provenance: {
      repoUrl: 'https://example.com/edgefilter.git',
      revision: 'v1.2.0',
      importedBy: 'alice',
      importedAt: 1700000000000,
      classification: 'bad',
    },
    lifecycle_state: 'dev',
    review: { decision: 'pending' },
    artifacts: {
      x86_64: {
        buildStatus: 'succeeded',
        s3Key: 'workflow-plugins/custom/uc-1/x86_64/edgefilter.so',
        checksum: 'deadbeefcafe0123',
        signature: 'c2lnbmF0dXJl',
      },
    },
    component: {},
    source_s3_prefix: 'plugin-sources/uc-1/p-1/2/',
    created_by: 'alice',
    created_at: 1700000000000,
    updated_at: 1700000000000,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { role: 'PortalAdmin' } });
  listPendingReviews.mockResolvedValue({ plugins: [PENDING_SUMMARY], count: 1 });
  getVersion.mockResolvedValue(PENDING_DETAIL);
  getVersionSource.mockResolvedValue({
    files: [
      { file: 'meson.build', size: 120 },
      { file: 'src/edgefilter.c', size: 2048 },
    ],
  });
  reviewVersion.mockResolvedValue({ plugin: PENDING_DETAIL.plugin });
});

describe('ReviewQueue', () => {
  it('is PortalAdmin-only', () => {
    useAuthMock.mockReturnValue({ user: { role: 'UseCaseAdmin' } });
    render(<ReviewQueue />);
    expect(screen.getByText('PortalAdmin only')).toBeInTheDocument();
    expect(listPendingReviews).not.toHaveBeenCalled();
  });

  it('lists pending records with their classification (10.2, 15.6)', async () => {
    render(<ReviewQueue />);
    await screen.findByText('edgefilter');
    expect(screen.getByText('Classification')).toBeInTheDocument();
    expect(screen.getByText('bad')).toBeInTheDocument();
    expect(screen.getByText('v2')).toBeInTheDocument();
    expect(screen.getByText('imported')).toBeInTheDocument();
  });

  it('shows full provenance, classification, checksums, signatures, and source inspection (10.2, 15.6)', async () => {
    render(<ReviewQueue />);
    await screen.findByText('edgefilter');
    fireEvent.click(screen.getByRole('button', { name: 'edgefilter' }));

    await screen.findByText('Review: edgefilter v2');

    // Provenance: repository URL/revision, importing user, timestamps.
    expect(screen.getByText('Repository URL')).toBeInTheDocument();
    expect(screen.getByText('https://example.com/edgefilter.git')).toBeInTheDocument();
    expect(screen.getByText('Revision')).toBeInTheDocument();
    expect(screen.getByText('v1.2.0')).toBeInTheDocument();
    expect(screen.getByText('Imported by')).toBeInTheDocument();
    // 'alice' appears as both the creating and the importing user.
    expect(screen.getAllByText('alice').length).toBeGreaterThan(0);

    // Classification alongside the other provenance details (15.6).
    expect(screen.getAllByText('bad').length).toBeGreaterThan(0);

    // Per-arch artifact checksums and signatures.
    expect(screen.getByText('Checksum (SHA-256)')).toBeInTheDocument();
    expect(screen.getByText('deadbeefcafe0123')).toBeInTheDocument();
    expect(screen.getByText('Signature')).toBeInTheDocument();
    expect(screen.getByText('c2lnbmF0dXJl')).toBeInTheDocument();

    // Source inspection with the record's source files.
    expect(screen.getByText('Source inspection')).toBeInTheDocument();
    await waitFor(() => expect(getVersionSource).toHaveBeenCalledWith('p-1', 2));

    // Approve and reject actions are offered (the hidden confirmation
    // modal also renders a Reject/Cancel pair, hence getAllByRole).
    expect(screen.getAllByRole('button', { name: 'Approve' }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: 'Reject' }).length).toBeGreaterThan(0);
  });

  it('records an approve decision through the review endpoint (10.2)', async () => {
    render(<ReviewQueue />);
    await screen.findByText('edgefilter');
    fireEvent.click(screen.getByRole('button', { name: 'edgefilter' }));
    await screen.findByText('Review: edgefilter v2');

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    // Confirmation modal: confirm the approval.
    await screen.findByText(/Record the approved decision/);
    const confirmButtons = screen.getAllByRole('button', { name: 'Approve' });
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() =>
      expect(reviewVersion).toHaveBeenCalledWith('p-1', 2, 'approved', undefined)
    );
    // The queue reloads after the decision.
    await waitFor(() => expect(listPendingReviews).toHaveBeenCalledTimes(2));
  });
});
