/**
 * The reference files of a list-form Tuning_Sample, in request order
 * (inspection-format nodes, design §10.4).
 *
 * An image shows as an image with its label ("Reference 1", …) as the
 * caption and the alt text. A document shows as an external link to its
 * presigned URL, opening in a new tab, with the text
 * "{label} · {FORMAT} · {size}", for example "Reference 2 · PDF · 178 KB".
 * The Samples and Compare tabs render this list in place of their single
 * reference image whenever a sample carries `references`.
 */
import { Box, Link, SpaceBetween } from '@cloudscape-design/components';
import { formatReferenceSize } from '../../utils/referenceFiles';
import type { ReferenceView } from './types';

/** "{label} · {FORMAT} · {size}" of a document reference. */
export function documentLinkText(reference: ReferenceView): string {
  const parts = [reference.label, reference.format.toUpperCase()];
  if (typeof reference.bytes === 'number') {
    parts.push(formatReferenceSize(reference.bytes));
  }
  return parts.join(' · ');
}

interface ReferenceListProps {
  references: ReferenceView[];
  /** Largest rendered image height, in pixels. */
  maxImageHeight?: number;
}

export default function ReferenceList({
  references,
  maxImageHeight = 220,
}: ReferenceListProps) {
  return (
    <SpaceBetween size="xs">
      {references.map((reference) =>
        reference.kind === 'document' ? (
          <Box
            key={`${reference.index}-${reference.label}`}
            data-testid={`reference-${reference.index}`}
          >
            {reference.url ? (
              <Link href={reference.url} external>
                {documentLinkText(reference)}
              </Link>
            ) : (
              <Box variant="p" color="text-status-inactive">
                {documentLinkText(reference)}
              </Box>
            )}
          </Box>
        ) : (
          <SpaceBetween
            key={`${reference.index}-${reference.label}`}
            size="xxs"
          >
            <Box variant="awsui-key-label">{reference.label}</Box>
            {reference.url ? (
              <img
                src={reference.url}
                alt={reference.label}
                style={{
                  maxWidth: '100%',
                  maxHeight: maxImageHeight,
                  objectFit: 'contain',
                }}
              />
            ) : (
              <Box variant="p" color="text-status-inactive">
                Image unavailable
              </Box>
            )}
          </SpaceBetween>
        )
      )}
    </SpaceBetween>
  );
}
