/**
 * BucketNotAllowedNotice (spec: portal-usecase-bucket-config, design §10.2,
 * tasks 7.5).
 *
 * The notice renders a refused bucket by its `error_code` and, for
 * `bucket_not_eligible`, its `reason`: the tag requirement with the
 * `aws s3control tag-resource` command, the ABAC / bucket-policy instruction,
 * the other-use-case message, the portal-bucket message without any action,
 * the server remedy for the remaining reasons, and the format messages for a
 * malformed URI or name. The "Allow for this use case" action is offered to a
 * UseCaseAdmin / PortalAdmin only, calls PUT /usecases/{id} with the
 * additional_s3_buckets_add body, switches to the reason branch on a 400
 * `bucket_not_eligible`, renders a 403 as the UseCaseAdmin message and calls
 * `onAllowed` on success.
 *
 * The fast-check property states that the rendered branch is a function of
 * (`error_code`, `reason`) only: random message, remedy and bucket text never
 * change it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as fc from 'fast-check';
import BucketNotAllowedNotice, {
  ALLOW_ACTION_LABEL,
  USECASE_ADMIN_REQUIRED_MESSAGE,
  bucketNoticeBranch,
} from './BucketNotAllowedNotice';
import { ApiError } from '../services/api';

const { allowBucketForUsecase, getMyUsecases, useAuthMock } = vi.hoisted(() => ({
  allowBucketForUsecase: vi.fn(),
  getMyUsecases: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    apiService: { allowBucketForUsecase, getMyUsecases },
  };
});

vi.mock('../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../contexts/AuthContext')>();
  return { ...actual, useAuth: useAuthMock };
});

const USECASE_ID = 'uc-1';

/** The flat 400 body the backend answers with, thrown as the client does. */
function bucketError(
  errorCode: string,
  extra: Record<string, unknown> = {},
  status = 400
): ApiError {
  const body = { error: `refused: ${errorCode}`, error_code: errorCode, ...extra };
  return new ApiError(body.error, status, undefined, body);
}

function grantRole(role: string | null, superUser = false) {
  getMyUsecases.mockResolvedValue({
    usecases: role ? [{ usecase_id: USECASE_ID, name: 'Line 3', user_role: role }] : [],
    total_count: role ? 1 : 0,
    is_super_user: superUser,
  });
}

function renderNotice(err: unknown, onAllowed = vi.fn(), usecaseId: string | null = USECASE_ID) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <BucketNotAllowedNotice err={err} usecaseId={usecaseId} onAllowed={onAllowed} />
    </QueryClientProvider>
  );
  return { ...utils, onAllowed };
}

function renderedBranch(container: HTMLElement): string | null {
  return container.querySelector('[data-bucket-notice-branch]')?.getAttribute('data-bucket-notice-branch') ?? null;
}

const actionButton = () => screen.queryByRole('button', { name: ALLOW_ACTION_LABEL });

/** Let the permissions query settle before asserting the action's absence. */
async function settlePermissions() {
  await waitFor(() => expect(getMyUsecases).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { user_id: 'u-1', role: 'Viewer' } });
  grantRole('UseCaseAdmin');
});

describe('bucket_not_allowed', () => {
  it('names the bucket and offers the action to a UseCaseAdmin', async () => {
    renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign', remedy: 'server remedy text' }));

    expect(screen.getByText('Bucket foreign is not allowed for this use case')).toBeInTheDocument();
    expect(screen.getByText('server remedy text')).toBeInTheDocument();
    await waitFor(() => expect(actionButton()).toBeInTheDocument());
  });

  it('offers the action to a PortalAdmin without a per-use-case row', async () => {
    grantRole(null, true);
    renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign' }));

    await waitFor(() => expect(actionButton()).toBeInTheDocument());
  });

  it('hides the action from a Viewer and from a DataScientist', async () => {
    for (const role of ['Viewer', 'DataScientist']) {
      grantRole(role);
      const { unmount } = renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign' }));
      await settlePermissions();
      expect(actionButton()).not.toBeInTheDocument();
      unmount();
    }
  });

  it('hides the action when no use case is known', async () => {
    renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign' }), vi.fn(), null);
    await settlePermissions();
    expect(actionButton()).not.toBeInTheDocument();
  });

  it('allows the bucket through PUT /usecases/{id} and reports success', async () => {
    allowBucketForUsecase.mockResolvedValue({
      message: 'ok',
      added: ['foreign'],
      already_present: [],
      removed: [],
      not_present: [],
      allowed_s3_buckets: ['line3-bucket', 'foreign'],
    });
    const { onAllowed } = renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign' }));

    fireEvent.click(await screen.findByRole('button', { name: ALLOW_ACTION_LABEL }));

    await waitFor(() => expect(onAllowed).toHaveBeenCalledTimes(1));
    expect(allowBucketForUsecase).toHaveBeenCalledWith(USECASE_ID, 'foreign');
  });

  it('switches to the reason branch when the server answers bucket_not_eligible', async () => {
    allowBucketForUsecase.mockRejectedValue(
      bucketError('bucket_not_eligible', { bucket: 'foreign', reason: 'no_tags', remedy: 'tag it' })
    );
    const { container, onAllowed } = renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign' }));

    fireEvent.click(await screen.findByRole('button', { name: ALLOW_ACTION_LABEL }));

    await waitFor(() => expect(renderedBranch(container)).toBe('tag_required'));
    expect(screen.getByText(/aws s3control tag-resource/)).toBeInTheDocument();
    expect(onAllowed).not.toHaveBeenCalled();
    expect(actionButton()).not.toBeInTheDocument();
  });

  it('renders a 403 as the UseCaseAdmin message', async () => {
    allowBucketForUsecase.mockRejectedValue(
      new ApiError('Insufficient permissions', 403, undefined, { error: 'Insufficient permissions' })
    );
    const { onAllowed } = renderNotice(bucketError('bucket_not_allowed', { bucket: 'foreign' }));

    fireEvent.click(await screen.findByRole('button', { name: ALLOW_ACTION_LABEL }));

    expect(await screen.findByText(USECASE_ADMIN_REQUIRED_MESSAGE)).toBeInTheDocument();
    expect(onAllowed).not.toHaveBeenCalled();
  });
});

describe('bucket_not_eligible by reason', () => {
  it.each(['no_tags', 'tag_missing'])('%s shows the S3 Control tagging command', (reason) => {
    const { container } = renderNotice(
      bucketError('bucket_not_eligible', { bucket: 'raw-bucket', reason, remedy: 'REMEDY_TAG' })
    );

    expect(renderedBranch(container)).toBe('tag_required');
    expect(screen.getByText('Bucket raw-bucket is not tagged for the portal')).toBeInTheDocument();
    expect(screen.getByText(/aws s3control tag-resource --account-id <bucket owner account> --resource-arn arn:aws:s3:::raw-bucket --tags Key=dda-portal:managed,Value=true/)).toBeInTheDocument();
    expect(screen.getByText('REMEDY_TAG')).toBeInTheDocument();
    expect(actionButton()).not.toBeInTheDocument();
  });

  it('tagging_unreadable explains ABAC, the bucket policy and the allowlist', () => {
    const { container } = renderNotice(
      bucketError('bucket_not_eligible', { bucket: 'locked', reason: 'tagging_unreadable', remedy: 'R' })
    );

    expect(renderedBranch(container)).toBe('tagging_unreadable');
    expect(screen.getByText('The portal could not read the tags of bucket locked')).toBeInTheDocument();
    expect(screen.getByText(/put-bucket-abac/)).toBeInTheDocument();
    expect(screen.getByText(/bucket policy/)).toBeInTheDocument();
    expect(screen.getByText(/allowlist/)).toBeInTheDocument();
  });

  it('bucket_configured_on_other_usecase names the PortalAdmin and shows the server remedy', async () => {
    const { container } = renderNotice(
      bucketError('bucket_not_eligible', {
        bucket: 'shared',
        reason: 'bucket_configured_on_other_usecase',
        remedy: 'This bucket is tagged for a use case that no longer exists.',
      })
    );

    expect(renderedBranch(container)).toBe('other_usecase');
    expect(
      screen.getByText('Bucket shared is configured on or tagged for another use case')
    ).toBeInTheDocument();
    expect(screen.getByText('A PortalAdmin can allow it for this use case too.')).toBeInTheDocument();
    expect(
      screen.getByText('This bucket is tagged for a use case that no longer exists.')
    ).toBeInTheDocument();
    // A UseCaseAdmin gets no action here: the server would refuse again.
    await settlePermissions();
    expect(actionButton()).not.toBeInTheDocument();
  });

  it('bucket_configured_on_other_usecase offers the action to a PortalAdmin (server override)', async () => {
    grantRole(null, true);
    renderNotice(
      bucketError('bucket_not_eligible', { bucket: 'shared', reason: 'bucket_configured_on_other_usecase' })
    );

    await waitFor(() => expect(actionButton()).toBeInTheDocument());
  });

  it('portal_bucket renders no action, even for a PortalAdmin', async () => {
    grantRole(null, true);
    const { container } = renderNotice(
      bucketError('bucket_not_eligible', { bucket: 'portal-artifacts', reason: 'portal_bucket', remedy: 'R' })
    );

    expect(renderedBranch(container)).toBe('portal_bucket');
    expect(
      screen.getByText("The portal's own buckets cannot be allowed for a use case")
    ).toBeInTheDocument();
    await settlePermissions();
    expect(actionButton()).not.toBeInTheDocument();
  });

  it.each([
    'credentials_unavailable',
    'bucket_not_found',
    'account_mismatch',
    'too_many_buckets',
  ])('%s shows the server remedy', (reason) => {
    const { container } = renderNotice(
      bucketError('bucket_not_eligible', { bucket: 'b', reason, remedy: `remedy for ${reason}` })
    );

    expect(renderedBranch(container)).toBe('server_remedy');
    expect(screen.getByText(`remedy for ${reason}`)).toBeInTheDocument();
    expect(actionButton()).not.toBeInTheDocument();
  });

  it('an unknown reason falls back to the generic not-eligible heading', () => {
    const { container } = renderNotice(
      bucketError('bucket_not_eligible', { bucket: 'b', reason: 'something_new', remedy: 'R' })
    );

    expect(renderedBranch(container)).toBe('not_eligible');
    expect(screen.getByText('Bucket b cannot be allowed for this use case')).toBeInTheDocument();
  });
});

describe('format errors', () => {
  it('invalid_s3_uri explains the s3://bucket/key form', () => {
    const { container } = renderNotice(bucketError('invalid_s3_uri'));
    expect(renderedBranch(container)).toBe('invalid_uri');
    expect(screen.getByText('This is not a valid S3 URI')).toBeInTheDocument();
  });

  it('invalid_bucket_name names the bucket and the grammar', () => {
    const { container } = renderNotice(bucketError('invalid_bucket_name', { bucket: 'Bad_Name' }));
    expect(renderedBranch(container)).toBe('invalid_name');
    expect(screen.getByText('Bad_Name is not a valid bucket name')).toBeInTheDocument();
  });

  it('a non-bucket error renders its message without a branch', () => {
    const { container } = renderNotice(new Error('plain failure'));
    expect(renderedBranch(container)).toBe('unknown');
    expect(screen.getByText('The bucket was refused')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- property

/** Oracle restated locally: the branch per (error_code, reason). */
function oracleBranch(code: string, reason: string | undefined): string {
  if (code === 'bucket_not_allowed') return 'not_allowed';
  if (code === 'invalid_s3_uri') return 'invalid_uri';
  if (code === 'invalid_bucket_name') return 'invalid_name';
  if (code !== 'bucket_not_eligible') return 'unknown';
  switch (reason) {
    case 'no_tags':
    case 'tag_missing':
      return 'tag_required';
    case 'tagging_unreadable':
      return 'tagging_unreadable';
    case 'bucket_configured_on_other_usecase':
      return 'other_usecase';
    case 'portal_bucket':
      return 'portal_bucket';
    case 'credentials_unavailable':
    case 'bucket_not_found':
    case 'account_mismatch':
    case 'too_many_buckets':
      return 'server_remedy';
    default:
      return 'not_eligible';
  }
}

const KNOWN_REASONS = [
  'no_tags',
  'tag_missing',
  'tagging_unreadable',
  'bucket_configured_on_other_usecase',
  'portal_bucket',
  'credentials_unavailable',
  'bucket_not_found',
  'account_mismatch',
  'too_many_buckets',
];

const codeArb = fc.oneof(
  fc.constantFrom('bucket_not_allowed', 'bucket_not_eligible', 'invalid_s3_uri', 'invalid_bucket_name'),
  fc.string({ minLength: 1, maxLength: 24 })
);
const reasonArb = fc.oneof(
  fc.constantFrom(...KNOWN_REASONS),
  fc.string({ maxLength: 24 }),
  fc.constant(undefined)
);
const textArb = fc.string({ maxLength: 60 });

describe('Property: the rendered branch depends only on (error_code, reason)', () => {
  it('random message, remedy and bucket text never change the branch', () => {
    fc.assert(
      fc.property(codeArb, reasonArb, textArb, textArb, textArb, (code, reason, message, remedy, bucket) => {
        cleanup();
        const body: Record<string, unknown> = { error: message, error_code: code };
        if (reason !== undefined) body.reason = reason;
        if (remedy) body.remedy = remedy;
        if (bucket) body.bucket = bucket;
        const err = new ApiError(message, 400, undefined, body);

        const { container, unmount } = renderNotice(err);
        const branch = renderedBranch(container);
        unmount();

        expect(branch).toBe(oracleBranch(code, reason));
        // The pure decision function agrees with the DOM.
        const typedCode = ['bucket_not_allowed', 'bucket_not_eligible', 'invalid_s3_uri', 'invalid_bucket_name'].includes(code)
          ? (code as Parameters<typeof bucketNoticeBranch>[0])
          : undefined;
        expect(bucketNoticeBranch(typedCode, reason)).toBe(branch);
      }),
      { numRuns: 25 }
    );
  });
});
