/**
 * WinnerPodium — the celebration rendering of a Completed labeling
 * job's Podium_Ranking (labeling-job-cleanup-work-stealing-and-podium
 * Requirements 8.4, 8.5).
 *
 * Shared by both podium surfaces: the admin job detail page
 * (`LabelingDetail.tsx`, Req 8.1) and the Labeler_Interface completion
 * view (`LabelerWorkspace.tsx`, Req 8.3). Purely presentational — the
 * ranking itself is the backend's shared `podium_ranking` pure function;
 * this component renders whatever entries the payload carried.
 *
 * - An empty entry list renders nothing at all (Req 8.5).
 * - Otherwise the classic podium column layout: 2nd | 1st | 3rd, with
 *   1st place tallest and most prominent (Req 8.4). Entries that don't
 *   exist (fewer than three submitters) leave their column out.
 * - Each Podium_Entry shows its medal (🥇🥈🥉), its place as text
 *   ("1st place" — not just the visual height), its display name
 *   (`email ?? user_id`), and its Submitted count as text (Req 8.4).
 *
 * Testids: `winner-podium`, `podium-place-1`, `podium-place-2`,
 * `podium-place-3`.
 */
import Box from '@cloudscape-design/components/box';
import type { PodiumEntry } from '../../services/api';

export interface WinnerPodiumProps {
  /** Ranked Podium_Entries (places 1-3) from the podium payload. */
  entries: PodiumEntry[];
}

/** Medal emoji per place, decorative beside the textual place. */
const MEDALS: Record<1 | 2 | 3, string> = { 1: '🥇', 2: '🥈', 3: '🥉' };
/** Ordinal wording per place — the accessible, textual ranking. */
const PLACE_LABELS: Record<1 | 2 | 3, string> = {
  1: '1st place',
  2: '2nd place',
  3: '3rd place',
};
/** Column (pedestal) height per place: 1st tallest (Req 8.4). */
const PEDESTAL_HEIGHTS: Record<1 | 2 | 3, number> = { 1: 96, 2: 68, 3: 48 };
/** Pedestal fill per place: gold, silver, bronze. */
const PEDESTAL_COLORS: Record<1 | 2 | 3, string> = {
  1: '#f2c94c',
  2: '#c0c4cc',
  3: '#d49a6a',
};

/** One podium column: medal, name, count, and the place pedestal. */
function PodiumColumn({ entry }: { entry: PodiumEntry }) {
  const displayName = entry.email ?? entry.user_id;
  const isFirst = entry.place === 1;
  return (
    <div
      data-testid={`podium-place-${entry.place}`}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'flex-end',
        gap: 4,
        minWidth: 120,
        maxWidth: 200,
      }}
    >
      <span aria-hidden="true" style={{ fontSize: isFirst ? 32 : 24 }}>
        {MEDALS[entry.place]}
      </span>
      <Box
        variant="strong"
        fontSize={isFirst ? 'heading-m' : 'body-m'}
        textAlign="center"
      >
        <span style={{ overflowWrap: 'anywhere' }}>{displayName}</span>
      </Box>
      <Box variant="small" textAlign="center">
        {entry.submitted} submitted
      </Box>
      <div
        style={{
          width: '100%',
          height: PEDESTAL_HEIGHTS[entry.place],
          background: PEDESTAL_COLORS[entry.place],
          borderRadius: '4px 4px 0 0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Box variant="strong">{PLACE_LABELS[entry.place]}</Box>
      </div>
    </div>
  );
}

export default function WinnerPodium({ entries }: WinnerPodiumProps) {
  // Empty or absent podium data renders no podium at all (Req 8.5).
  if (entries.length === 0) return null;

  const byPlace = (place: 1 | 2 | 3): PodiumEntry | undefined =>
    entries.find((entry) => entry.place === place);

  // Classic column order: 2nd | 1st | 3rd (Req 8.4).
  const columns = [byPlace(2), byPlace(1), byPlace(3)].filter(
    (entry): entry is PodiumEntry => entry !== undefined
  );

  return (
    <div data-testid="winner-podium" role="group" aria-label="Winner podium">
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          justifyContent: 'center',
          gap: 8,
          paddingTop: 8,
        }}
      >
        {columns.map((entry) => (
          <PodiumColumn key={entry.place} entry={entry} />
        ))}
      </div>
    </div>
  );
}
