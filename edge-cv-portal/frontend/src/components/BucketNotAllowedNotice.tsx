/**
 * BucketNotAllowedNotice — renders a refused bucket (spec:
 * portal-usecase-bucket-config, design §10.2).
 *
 * Every S3 access the portal performs for a use case is bound to the use
 * case's allowed buckets (configured + additional). When a handler refuses a
 * caller-supplied bucket it answers a flat 400 body with `error_code`
 * (`bucket_not_allowed`, `bucket_not_eligible`, `invalid_s3_uri`,
 * `invalid_bucket_name`), the `bucket`, and for `bucket_not_eligible` a
 * `reason` token plus a `remedy` sentence. This component branches STRICTLY
 * on (`error_code`, `reason`) — the server's prose (`error`, `remedy`) is
 * displayed as secondary text and never decides the branch — and offers the
 * "Allow for this use case" action where the caller may update the use case.
 *
 * The action calls PUT /usecases/{id} {additional_s3_buckets_add: [bucket]}:
 * a 400 `bucket_not_eligible` answer switches the notice to that reason's
 * branch, a 403 renders "A UseCaseAdmin can allow buckets for this use case",
 * success calls `onAllowed()` so the caller can retry what it was doing.
 */
import { useEffect, useState } from 'react';
import { Alert, Box, Button, SpaceBetween } from '@cloudscape-design/components';
import { ApiError, apiService } from '../services/api';
import { useUsecasePermissions } from '../hooks/useUsecasePermissions';
import {
  BucketErrorCode,
  bucketErrorCode,
  bucketErrorFields,
} from '../utils/bucketErrors';

/** The rendered branch; a pure function of (error_code, reason). */
export type BucketNoticeBranch =
  | 'not_allowed'
  | 'tag_required'
  | 'tagging_unreadable'
  | 'other_usecase'
  | 'portal_bucket'
  | 'server_remedy'
  | 'not_eligible'
  | 'invalid_uri'
  | 'invalid_name'
  | 'unknown';

/** Reasons whose heading is a short label and whose body is the server remedy. */
const SERVER_REMEDY_REASONS: Record<string, string> = {
  credentials_unavailable: 'The portal could not assume the role configured for this bucket',
  bucket_not_found: 'The bucket does not exist or is not visible to the portal',
  account_mismatch: 'Buckets created in the portal account can only be allowed for single-account use cases',
  too_many_buckets: 'This use case already has the maximum number of additional buckets',
};

/**
 * Decide the branch from the two tokens only. Exported so tests (and the
 * fast-check property) can state the oracle without rendering.
 */
export function bucketNoticeBranch(
  code: BucketErrorCode | undefined,
  reason: string | undefined
): BucketNoticeBranch {
  switch (code) {
    case 'bucket_not_allowed':
      return 'not_allowed';
    case 'bucket_not_eligible':
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
    case 'invalid_s3_uri':
      return 'invalid_uri';
    case 'invalid_bucket_name':
      return 'invalid_name';
    default:
      return 'unknown';
  }
}

/** The S3 Control tagging command an AWS administrator runs (design §1.2). */
export function tagResourceCommand(bucket: string | undefined): string {
  const name = bucket || '<bucket>';
  return `aws s3control tag-resource --account-id <bucket owner account> --resource-arn arn:aws:s3:::${name} --tags Key=dda-portal:managed,Value=true`;
}

export const ALLOW_ACTION_LABEL = 'Allow for this use case';
export const USECASE_ADMIN_REQUIRED_MESSAGE =
  'A UseCaseAdmin can allow buckets for this use case';

interface BucketNotAllowedNoticeProps {
  /** The caught error (an ApiError carrying the bucket error body). */
  err: unknown;
  /** The use case the operation ran for; the action needs it. */
  usecaseId?: string | null;
  /** Called after the bucket was allowed so the caller can retry. */
  onAllowed?: () => void;
}

export default function BucketNotAllowedNotice({
  err,
  usecaseId,
  onAllowed,
}: BucketNotAllowedNoticeProps): JSX.Element {
  const { canUpdateUsecase, isPortalAdmin } = useUsecasePermissions();
  // A refused "Allow" answer replaces the rendered error (design §10.2).
  const [current, setCurrent] = useState<unknown>(err);
  const [actionError, setActionError] = useState<string | null>(null);
  const [allowing, setAllowing] = useState(false);

  useEffect(() => {
    setCurrent(err);
    setActionError(null);
  }, [err]);

  const code = bucketErrorCode(current);
  const { bucket, reason, remedy, message } = bucketErrorFields(current);
  const branch = bucketNoticeBranch(code, reason);
  const name = bucket || 'This bucket';

  const handleAllow = async () => {
    if (!usecaseId || !bucket) return;
    setAllowing(true);
    setActionError(null);
    try {
      await apiService.allowBucketForUsecase(usecaseId, bucket);
      onAllowed?.();
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setActionError(USECASE_ADMIN_REQUIRED_MESSAGE);
      } else if (bucketErrorCode(e) === 'bucket_not_eligible') {
        setCurrent(e);
      } else {
        setActionError(e instanceof Error ? e.message : 'Failed to allow the bucket');
      }
    } finally {
      setAllowing(false);
    }
  };

  // The action is offered where the server would accept the add: a bucket
  // outside the set for a UseCaseAdmin/PortalAdmin, and a bucket configured
  // on or tagged for another use case for a PortalAdmin (the server's
  // sharing override). Never for the portal's own buckets.
  const showAction =
    !!usecaseId &&
    !!bucket &&
    ((branch === 'not_allowed' && canUpdateUsecase(usecaseId)) ||
      (branch === 'other_usecase' && isPortalAdmin));

  let heading: string;
  let body: JSX.Element | null = null;
  switch (branch) {
    case 'not_allowed':
      heading = `Bucket ${name} is not allowed for this use case`;
      body = (
        <Box>
          Only the use case's configured buckets and its additional buckets can be
          used. A UseCaseAdmin can allow this bucket, or place the object in the use
          case's configured bucket.
        </Box>
      );
      break;
    case 'tag_required':
      heading = `Bucket ${name} is not tagged for the portal`;
      body = (
        <SpaceBetween size="xs">
          <Box>
            Before a use case can use a bucket, an AWS administrator must tag it{' '}
            <code>dda-portal:managed=true</code> with the S3 Control TagResource API
            (console, or the command below), then retry.
          </Box>
          <Box variant="code" fontSize="body-s">
            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
              {tagResourceCommand(bucket)}
            </pre>
          </Box>
        </SpaceBetween>
      );
      break;
    case 'tagging_unreadable':
      heading = `The portal could not read the tags of bucket ${name}`;
      body = (
        <Box>
          If the bucket is not tagged, tag it <code>dda-portal:managed=true</code> with
          the S3 Control TagResource API (<code>{tagResourceCommand(bucket)}</code>).
          If it is tagged, enable ABAC on it (
          <code>aws s3api put-bucket-abac --bucket {bucket || '<bucket>'} --abac-status Status=Enabled</code>
          ) or grant s3:GetBucketTagging to the portal's role in the bucket policy; on a
          single-account portal deployed with a data-bucket allowlist, add the bucket to
          that list and redeploy. Then retry.
        </Box>
      );
      break;
    case 'other_usecase':
      heading = `Bucket ${name} is configured on or tagged for another use case`;
      body = <Box>A PortalAdmin can allow it for this use case too.</Box>;
      break;
    case 'portal_bucket':
      heading = "The portal's own buckets cannot be allowed for a use case";
      body = (
        <Box>
          {bucket ? `${bucket} is one of the portal's own buckets (artifacts, components). ` : ''}
          Choose a bucket that belongs to the use case.
        </Box>
      );
      break;
    case 'server_remedy':
      heading = SERVER_REMEDY_REASONS[reason as string] || `Bucket ${name} cannot be allowed`;
      break;
    case 'not_eligible':
      heading = `Bucket ${name} cannot be allowed for this use case`;
      break;
    case 'invalid_uri':
      heading = 'This is not a valid S3 URI';
      body = (
        <Box>
          Enter the location as <code>s3://bucket/key</code>: the bucket name and the
          object key, no other schemes or hosts.
        </Box>
      );
      break;
    case 'invalid_name':
      heading = bucket ? `${bucket} is not a valid bucket name` : 'This is not a valid bucket name';
      body = (
        <Box>
          Bucket names are 3–63 lowercase letters, digits, dots and hyphens, starting and
          ending with a letter or digit.
        </Box>
      );
      break;
    default:
      heading = message || 'The bucket was refused';
  }

  return (
    <div data-bucket-notice-branch={branch} data-bucket-notice-code={code || ''}>
      <Alert
        type="error"
        header={heading}
        action={
          showAction ? (
            <Button onClick={handleAllow} loading={allowing} disabled={allowing}>
              {ALLOW_ACTION_LABEL}
            </Button>
          ) : undefined
        }
      >
        <SpaceBetween size="xs">
          {body}
          {remedy && (
            <Box color="text-body-secondary" fontSize="body-s">
              {remedy}
            </Box>
          )}
          {actionError && <Box color="text-status-error">{actionError}</Box>}
        </SpaceBetween>
      </Alert>
    </div>
  );
}
