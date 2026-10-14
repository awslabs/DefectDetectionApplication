/**
 * Admin_Review page for Skip_Verification_Mode jobs (dda-data-labeling
 * task 16.6, route `/labeling/:jobId/review`).
 *
 * Grid of every dataset image with its auto-labeled result or failed
 * status (Requirement 9.5). Succeeded items render the image with the
 * annotation visualized (classification label badge, bounding boxes
 * overlaid on the image, mask region count for segmentation) and
 * Accept/Reject toggles whose decisions are batch-saved through
 * `POST /labeling/{id}/review/decisions` (Requirement 9.6). Failed items
 * are flagged ineligible with their recorded auto-label error and carry
 * no accept control (Requirement 9.10). The Finalize button mirrors the
 * server-side guardrails client-side: it is blocked while any succeeded
 * item is undecided (showing the undecided count, Requirement 9.7) or
 * while zero items are accepted (Requirement 9.8). Decisions stay
 * mutable until finalization; once `review_finalized` the page is
 * read-only (Requirement 9.6).
 *
 * The page is UI-gated to UseCaseAdmin/PortalAdmin through `RequireRole`
 * in App.tsx; server-side authorization on the review APIs remains the
 * ultimate authority (Requirement 9.1).
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  Cards,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import {
  apiService,
  DdaAnnotation,
  ReviewItem,
} from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';

type Decision = 'accepted' | 'rejected';

/** Short display name for a dataset image S3 key. */
function imageBasename(imageKey: string): string {
  const parts = imageKey.split('/');
  return parts[parts.length - 1] || imageKey;
}

/**
 * Read-only visualization of an auto-labeled annotation over the item's
 * image (Requirement 9.5): classification renders a label badge,
 * object-detection boxes are overlaid on the image using the annotation's
 * source pixel dimensions, and segmentation shows the mask region count.
 */
function AnnotationPreview({ item }: { item: ReviewItem }) {
  const annotation: DdaAnnotation | undefined = item.annotation;

  const boxes = annotation?.boxes ?? [];
  const regions = annotation?.regions ?? [];
  const width = annotation?.image_width;
  const height = annotation?.image_height;
  // Boxes are in source-image pixel coordinates; with the source
  // dimensions known they scale onto the thumbnail as percentages.
  const canOverlayBoxes = boxes.length > 0 && !!width && !!height;

  return (
    <SpaceBetween size="xs">
      <div style={{ position: 'relative', display: 'inline-block', maxWidth: '100%' }}>
        {item.image_url ? (
          <img
            src={item.image_url}
            alt={imageBasename(item.image_key)}
            style={{ display: 'block', width: '100%', borderRadius: 4 }}
          />
        ) : (
          <Box color="text-body-secondary" padding="s">
            Image preview unavailable
          </Box>
        )}
        {item.image_url &&
          canOverlayBoxes &&
          boxes.map((box, i) => (
            <div
              key={i}
              role="img"
              aria-label={`Bounding box ${i + 1}${box.class ? ` (${box.class})` : ''}`}
              style={{
                position: 'absolute',
                left: `${(box.left / (width as number)) * 100}%`,
                top: `${(box.top / (height as number)) * 100}%`,
                width: `${(box.width / (width as number)) * 100}%`,
                height: `${(box.height / (height as number)) * 100}%`,
                border: '2px solid #e07941',
                boxSizing: 'border-box',
                pointerEvents: 'none',
              }}
            >
              {box.class && (
                <span
                  style={{
                    position: 'absolute',
                    top: -18,
                    left: -2,
                    background: '#e07941',
                    color: '#fff',
                    fontSize: 11,
                    lineHeight: '16px',
                    padding: '0 4px',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {box.class}
                </span>
              )}
            </div>
          ))}
      </div>

      {/* Classification result badge. */}
      {annotation?.label !== undefined && annotation?.label !== null && (
        <Badge color={annotation.label === 'anomaly' ? 'red' : 'green'}>
          {annotation.label}
        </Badge>
      )}

      {/* Object-detection class summary (also covers the no-dimensions
          fallback where boxes cannot be overlaid). */}
      {boxes.length > 0 && (
        <Box fontSize="body-s" color="text-body-secondary">
          {boxes.length} box{boxes.length === 1 ? '' : 'es'}
          {': '}
          {boxes.map((b) => b.class ?? 'unclassified').join(', ')}
        </Box>
      )}

      {/* Segmentation summary: region count per Requirement 9.5. */}
      {regions.length > 0 && (
        <Box fontSize="body-s" color="text-body-secondary">
          {regions.length} mask region{regions.length === 1 ? '' : 's'}
          {': '}
          {regions.map((r) => r.class ?? 'unclassified').join(', ')}
        </Box>
      )}
    </SpaceBetween>
  );
}

export default function AdminReview() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();

  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [finalized, setFinalized] = useState(false);

  // Locally edited, not-yet-saved decisions keyed by task_id. Only
  // entries that differ from the persisted decision are kept here.
  const [pendingDecisions, setPendingDecisions] = useState<
    Record<string, Decision>
  >({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [finalizing, setFinalizing] = useState(false);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);
  const [finalizeSuccess, setFinalizeSuccess] = useState(false);

  // Load every page of the review so the client-side finalize guardrails
  // (undecided count, zero accepted) are computed over the full result
  // set (Requirements 9.5, 9.7, 9.8).
  const loadReview = useCallback(async () => {
    if (!jobId) return;
    try {
      setLoading(true);
      setError(null);
      const all: ReviewItem[] = [];
      let nextToken: string | undefined;
      let reviewFinalized = false;
      do {
        const page = await apiService.getReview(jobId, nextToken);
        all.push(...(page.items || []));
        reviewFinalized = reviewFinalized || !!page.review_finalized;
        nextToken = page.next_token;
      } while (nextToken);
      setItems(all);
      setFinalized(reviewFinalized);
      setPendingDecisions({});
    } catch (err) {
      console.error('Failed to load admin review:', err);
      setError(getErrorMessage(err, 'Failed to load the review results'));
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    loadReview();
  }, [loadReview]);

  /** The decision currently in effect for an item: pending edit first. */
  const effectiveDecision = useCallback(
    (item: ReviewItem): Decision | undefined =>
      pendingDecisions[item.task_id] ?? item.decision,
    [pendingDecisions]
  );

  // Finalize guardrail counts over succeeded items only — failed items
  // are ineligible and never counted as undecided (Requirements 9.7,
  // 9.8, 9.10).
  const { succeededCount, failedCount, acceptedCount, rejectedCount, undecidedCount } =
    useMemo(() => {
      let succeeded = 0;
      let failed = 0;
      let accepted = 0;
      let rejected = 0;
      let undecided = 0;
      for (const item of items) {
        if (item.status !== 'succeeded') {
          failed += 1;
          continue;
        }
        succeeded += 1;
        const decision = pendingDecisions[item.task_id] ?? item.decision;
        if (decision === 'accepted') accepted += 1;
        else if (decision === 'rejected') rejected += 1;
        else undecided += 1;
      }
      return {
        succeededCount: succeeded,
        failedCount: failed,
        acceptedCount: accepted,
        rejectedCount: rejected,
        undecidedCount: undecided,
      };
    }, [items, pendingDecisions]);

  const dirtyCount = Object.keys(pendingDecisions).length;

  // Decisions stay mutable until the review is finalized (Requirement
  // 9.6); afterwards the page is read-only.
  const setDecision = useCallback(
    (item: ReviewItem, decision: Decision) => {
      if (finalized || item.status !== 'succeeded') return;
      setSaveError(null);
      setFinalizeError(null);
      setPendingDecisions((prev) => {
        const next = { ...prev };
        if (item.decision === decision) {
          // Back to the persisted value — nothing pending to save.
          delete next[item.task_id];
        } else {
          next[item.task_id] = decision;
        }
        return next;
      });
    },
    [finalized]
  );

  /** Batch-save the pending decisions (Requirement 9.6). */
  const saveDecisions = useCallback(async (): Promise<boolean> => {
    if (!jobId || dirtyCount === 0) return true;
    const toSave = { ...pendingDecisions };
    try {
      setSaving(true);
      setSaveError(null);
      await apiService.saveReviewDecisions(jobId, toSave);
      // Fold the saved decisions into the persisted item state.
      setItems((prev) =>
        prev.map((item) =>
          toSave[item.task_id]
            ? { ...item, decision: toSave[item.task_id] }
            : item
        )
      );
      setPendingDecisions((prev) => {
        const next = { ...prev };
        for (const taskId of Object.keys(toSave)) {
          if (next[taskId] === toSave[taskId]) delete next[taskId];
        }
        return next;
      });
      return true;
    } catch (err) {
      console.error('Failed to save review decisions:', err);
      setSaveError(getErrorMessage(err, 'Failed to save review decisions'));
      return false;
    } finally {
      setSaving(false);
    }
  }, [jobId, dirtyCount, pendingDecisions]);

  /**
   * Finalize the review (Requirements 9.7, 9.8): unsaved decisions are
   * saved first so the server evaluates the state shown on screen. The
   * button is disabled while the client-side guardrails fail, and any
   * server-side rejection (the authority) is surfaced unchanged.
   */
  const finalize = useCallback(async () => {
    if (!jobId) return;
    setFinalizeError(null);
    const saved = await saveDecisions();
    if (!saved) return;
    try {
      setFinalizing(true);
      await apiService.finalizeReview(jobId);
      setFinalized(true);
      setFinalizeSuccess(true);
    } catch (err) {
      console.error('Failed to finalize review:', err);
      setFinalizeError(getErrorMessage(err, 'Failed to finalize the review'));
    } finally {
      setFinalizing(false);
    }
  }, [jobId, saveDecisions]);

  const finalizeBlocked =
    undecidedCount > 0 || acceptedCount === 0 || items.length === 0;

  return (
    <Container
      header={
        <Header
          variant="h1"
          description="Accept or reject each auto-labeled result. Accepted results are written to the output manifest when the review is finalized."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => navigate(`/labeling/${jobId}`)}>
                Back to Job
              </Button>
              {!finalized && (
                <Button
                  onClick={saveDecisions}
                  loading={saving}
                  disabled={dirtyCount === 0 || saving || finalizing}
                >
                  Save Decisions{dirtyCount > 0 ? ` (${dirtyCount})` : ''}
                </Button>
              )}
              {!finalized && (
                <Button
                  variant="primary"
                  onClick={finalize}
                  loading={finalizing}
                  disabled={finalizeBlocked || loading || saving || finalizing}
                >
                  Finalize Review
                </Button>
              )}
            </SpaceBetween>
          }
        >
          Admin Review
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}
        {saveError && <Alert type="error">{saveError}</Alert>}
        {finalizeError && <Alert type="error">{finalizeError}</Alert>}

        {finalizeSuccess && (
          <Alert type="success">
            The review is finalized. The accepted results are being written
            to the output manifest.
          </Alert>
        )}

        {/* Read-only state after finalization (Requirement 9.6). */}
        {finalized && !finalizeSuccess && (
          <Alert type="info">
            This review has been finalized. Decisions can no longer be
            changed.
          </Alert>
        )}

        {/* Client-side mirrors of the server finalize guardrails
            (Requirements 9.7, 9.8). */}
        {!loading && !finalized && undecidedCount > 0 && (
          <Alert type="warning">
            {undecidedCount} auto-labeled image
            {undecidedCount === 1 ? ' has' : 's have'} no accept or reject
            decision yet. Every successfully auto-labeled image must be
            decided before the review can be finalized.
          </Alert>
        )}
        {!loading &&
          !finalized &&
          undecidedCount === 0 &&
          acceptedCount === 0 &&
          succeededCount > 0 && (
            <Alert type="warning">
              At least one accepted result is required to finalize the
              review.
            </Alert>
          )}

        {!loading && (
          <Box color="text-body-secondary" fontSize="body-s">
            {items.length} image{items.length === 1 ? '' : 's'} —{' '}
            {succeededCount} auto-labeled, {failedCount} failed;{' '}
            {acceptedCount} accepted, {rejectedCount} rejected,{' '}
            {undecidedCount} undecided
          </Box>
        )}

        <Cards
          trackBy="task_id"
          items={items}
          loading={loading}
          loadingText="Loading auto-label results"
          cardsPerRow={[
            { cards: 1 },
            { minWidth: 600, cards: 2 },
            { minWidth: 1000, cards: 3 },
          ]}
          cardDefinition={{
            header: (item: ReviewItem) => (
              <Box fontSize="heading-s" fontWeight="bold">
                {imageBasename(item.image_key)}
              </Box>
            ),
            sections: [
              {
                id: 'result',
                content: (item: ReviewItem) =>
                  item.status === 'succeeded' ? (
                    <AnnotationPreview item={item} />
                  ) : (
                    // Failed items are ineligible for acceptance and show
                    // the recorded error (Requirement 9.10).
                    <SpaceBetween size="xs">
                      <StatusIndicator type="error">
                        Auto-labeling failed — ineligible
                      </StatusIndicator>
                      {item.autolabel_error && (
                        <Box fontSize="body-s" color="text-body-secondary">
                          {item.autolabel_error}
                        </Box>
                      )}
                    </SpaceBetween>
                  ),
              },
              {
                id: 'decision',
                content: (item: ReviewItem) => {
                  if (item.status !== 'succeeded') {
                    return null;
                  }
                  const decision = effectiveDecision(item);
                  if (finalized) {
                    return decision === 'accepted' ? (
                      <StatusIndicator type="success">Accepted</StatusIndicator>
                    ) : decision === 'rejected' ? (
                      <StatusIndicator type="stopped">Rejected</StatusIndicator>
                    ) : (
                      <StatusIndicator type="pending">Undecided</StatusIndicator>
                    );
                  }
                  return (
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button
                        variant={decision === 'accepted' ? 'primary' : 'normal'}
                        onClick={() => setDecision(item, 'accepted')}
                        ariaLabel={`Accept ${imageBasename(item.image_key)}`}
                      >
                        Accept
                      </Button>
                      <Button
                        variant={decision === 'rejected' ? 'primary' : 'normal'}
                        onClick={() => setDecision(item, 'rejected')}
                        ariaLabel={`Reject ${imageBasename(item.image_key)}`}
                      >
                        Reject
                      </Button>
                      {decision === undefined && (
                        <StatusIndicator type="pending">
                          Undecided
                        </StatusIndicator>
                      )}
                    </SpaceBetween>
                  );
                },
              },
            ],
          }}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No auto-label results</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                This job has no auto-label results to review yet.
              </Box>
            </Box>
          }
        />
      </SpaceBetween>
    </Container>
  );
}
