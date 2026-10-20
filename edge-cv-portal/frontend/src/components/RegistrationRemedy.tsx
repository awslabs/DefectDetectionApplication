/**
 * Registration_Report remedies (spec: portal-authorization-and-iam-scoping,
 * design I5 "The Attach_Step"; task 8.1).
 *
 * Every response that attaches a bucket to a use case (use-case create, the
 * Edit modal's save, "Allow" on a bucket, bucket creation, the data-account
 * configure) reports per bucket whether the portal can use it and, when it
 * cannot yet, a `remedy` naming the one step left (tag the bucket, enable
 * ABAC on it, redeploy). The attach itself succeeded; the remedy is shown
 * beside the bucket so the administrator can finish it.
 */
import { Box, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import type { RegistrationReport } from '../services/api';

/** The buckets of a report that carry a remedy, in report order. */
export function registrationRemedies(
  report: RegistrationReport | null | undefined
): Array<{ bucket: string; remedy: string }> {
  if (!report) return [];
  return Object.entries(report)
    .filter(([, entry]) => typeof entry?.remedy === 'string' && entry.remedy.trim() !== '')
    .map(([bucket, entry]) => ({ bucket, remedy: entry.remedy as string }));
}

/** The remedy of one bucket in a report, or undefined when none is needed. */
export function registrationRemedyFor(
  report: RegistrationReport | null | undefined,
  bucket: string
): string | undefined {
  const remedy = report?.[bucket]?.remedy;
  return typeof remedy === 'string' && remedy.trim() !== '' ? remedy : undefined;
}

/** One bucket's remedy, rendered beside the bucket. */
export function BucketRemedy({ remedy }: { remedy: string }): JSX.Element {
  return (
    <Box fontSize="body-s">
      <StatusIndicator type="warning">Needs one more step</StatusIndicator> {remedy}
    </Box>
  );
}

/** Every remedy of a report, each beside its bucket; renders nothing when none is needed. */
export function RegistrationRemedies({
  report,
}: {
  report: RegistrationReport | null | undefined;
}): JSX.Element | null {
  const entries = registrationRemedies(report);
  if (entries.length === 0) return null;
  return (
    <SpaceBetween size="xxs">
      {entries.map(({ bucket, remedy }) => (
        <SpaceBetween key={bucket} direction="horizontal" size="xs" alignItems="center">
          <code>{bucket}</code>
          <BucketRemedy remedy={remedy} />
        </SpaceBetween>
      ))}
    </SpaceBetween>
  );
}
