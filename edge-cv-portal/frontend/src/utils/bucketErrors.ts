/**
 * Readers for the use-case bucket configuration error bodies (spec:
 * portal-usecase-bucket-config, design §1.2 / §10.1).
 *
 * The backend answers a refused bucket with a flat 400 body
 * `{error, error_code, bucket?, usecase_id?, purpose?, reason?, remedy?}`.
 * `services/api.ts` throws it as an `ApiError` whose `message` is `error`
 * and whose `details` is the whole parsed body, so the sibling fields are
 * read from `details` here. Everything the UI branches on is a token
 * (`error_code`, `reason`); the prose (`error`, `remedy`) is only displayed.
 */
import { ApiError } from '../services/api';

/** The error codes the bucket notice renders (design §10.1). */
export type BucketErrorCode =
  | 'bucket_not_allowed'
  | 'bucket_not_eligible'
  | 'invalid_s3_uri'
  | 'invalid_bucket_name';

export const BUCKET_ERROR_CODES: readonly BucketErrorCode[] = [
  'bucket_not_allowed',
  'bucket_not_eligible',
  'invalid_s3_uri',
  'invalid_bucket_name',
];

/** The `reason` tokens a `bucket_not_eligible` refusal carries (design §1.6). */
export type BucketEligibilityReason =
  | 'no_tags'
  | 'tag_missing'
  | 'tagging_unreadable'
  | 'bucket_configured_on_other_usecase'
  | 'portal_bucket'
  | 'credentials_unavailable'
  | 'bucket_not_found'
  | 'account_mismatch'
  | 'too_many_buckets';

export interface BucketErrorFields {
  bucket?: string;
  reason?: string;
  remedy?: string;
  /** The server's `error` message. */
  message?: string;
}

function detailString(err: ApiError, key: string): string | undefined {
  const value = err.details?.[key];
  return typeof value === 'string' && value ? value : undefined;
}

/**
 * The bucket error code of a caught error, or undefined when the error is
 * not an `ApiError` carrying one of the four codes.
 */
export function bucketErrorCode(err: unknown): BucketErrorCode | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const code = err.details?.error_code;
  return typeof code === 'string' && (BUCKET_ERROR_CODES as readonly string[]).includes(code)
    ? (code as BucketErrorCode)
    : undefined;
}

/** `bucket`, `reason`, `remedy` (and the message) of a bucket error body. */
export function bucketErrorFields(err: unknown): BucketErrorFields {
  if (!(err instanceof ApiError)) return {};
  return {
    bucket: detailString(err, 'bucket'),
    reason: detailString(err, 'reason'),
    remedy: detailString(err, 'remedy'),
    message: err.message || detailString(err, 'error'),
  };
}
