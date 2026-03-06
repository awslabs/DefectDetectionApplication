/**
 * S3 input validation utilities.
 *
 * Two kinds of S3 inputs exist in the portal:
 *  1. Bucket names  – just the name, no "s3://" prefix  (e.g. "my-bucket")
 *  2. S3 URIs       – full path with prefix             (e.g. "s3://my-bucket/path/file")
 */

/** Strip the s3:// prefix and return just the bucket name, or null if invalid. */
export function extractBucketName(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed.startsWith('s3://')) {
    // User pasted a URI — pull out the bucket name
    const parts = trimmed.replace('s3://', '').split('/');
    return parts[0] || null;
  }
  return trimmed || null;
}

/** Validate an S3 bucket name (not a URI). Returns error message or null. */
export function validateBucketName(value: string): string | null {
  if (!value) return null; // let required-field checks handle empty
  const name = value.trim();

  if (name.startsWith('s3://')) {
    return 'Enter just the bucket name without the s3:// prefix';
  }
  if (name.length < 3 || name.length > 63) {
    return 'Bucket name must be 3–63 characters';
  }
  if (!/^[a-z0-9][a-z0-9.\-]*[a-z0-9]$/.test(name)) {
    return 'Bucket name must start/end with a lowercase letter or number and contain only lowercase letters, numbers, hyphens, and periods';
  }
  if (/\.\./.test(name) || /--/.test(name)) {
    return 'Bucket name cannot contain consecutive periods or hyphens';
  }
  if (/^\d+\.\d+\.\d+\.\d+$/.test(name)) {
    return 'Bucket name cannot be formatted as an IP address';
  }
  return null;
}

/** Validate an S3 URI (s3://bucket/path). Returns error message or null. */
export function validateS3Uri(value: string): string | null {
  if (!value) return null;
  const trimmed = value.trim();

  if (!trimmed.startsWith('s3://')) {
    return 'S3 URI must start with s3://';
  }
  const withoutPrefix = trimmed.slice(5); // remove "s3://"
  if (!withoutPrefix || withoutPrefix === '/') {
    return 'S3 URI must include a bucket name after s3://';
  }
  const bucket = withoutPrefix.split('/')[0];
  if (!bucket) {
    return 'S3 URI must include a bucket name after s3://';
  }
  // Basic bucket name check within the URI
  if (!/^[a-z0-9][a-z0-9.\-]*[a-z0-9]$/.test(bucket) && bucket.length >= 3) {
    return `Invalid bucket name "${bucket}" in S3 URI`;
  }
  return null;
}
