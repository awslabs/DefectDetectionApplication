/**
 * Labeler workspace (dda-data-labeling task 16.5, route `/labeler`).
 *
 * Job list view: the jobs in which the signed-in Data_Labeler holds at
 * least one unsubmitted Task_Assignment, with submitted/remaining counts
 * (Requirements 7.1, 7.10). Selecting a job — or arriving with a `?job=`
 * query parameter from the notification email link — enters the
 * single-image labeling view.
 *
 * Labeling view: driven by `GET /labeler/jobs/{jobId}/next`. The image
 * canvas (AnnotationCanvas) sits center; the right rail shows the job's
 * instructions text and good/bad example thumbnails with a lightbox,
 * omitting absent items (Requirement 7.2). Submissions go through
 * `POST /labeler/tasks/{taskId}/submit`; success advances to the next
 * task and updates the submitted/remaining counts (Requirements 7.7,
 * 7.10), failure shows an error alert while retaining the annotation on
 * screen (Requirement 7.9). When no presentable tasks remain, a
 * completion message shows the submitted count and, if any, the withheld
 * count (Requirement 7.11). Presentation failures are reported and the
 * view continues to the next presentable task (Requirement 7.12).
 *
 * The AnnotationCanvas is remounted per task (`key={task_id}`) so
 * annotation state resets between tasks.
 */

import { useState, useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Container,
  Grid,
  Header,
  Modal,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import {
  ApiError,
  apiService,
  DdaAnnotation,
  LabelerJobSummary,
  LabelerNextTaskResponse,
} from '../../services/api';
import AnnotationCanvas, {
  LabelingModality,
} from '../../components/labeling/AnnotationCanvas';
import { getErrorMessage } from '../../utils/errorHandling';

/** A good/bad example image opened in the lightbox modal. */
interface LightboxImage {
  url: string;
  title: string;
}

/**
 * Thumbnail strip for the good/bad example images of the right rail
 * (Requirement 7.2). Rendered only when at least one URL exists; clicking
 * a thumbnail opens the simple lightbox modal.
 */
function ExampleThumbnails({
  title,
  urls,
  onOpen,
}: {
  title: string;
  urls: string[];
  onOpen: (image: LightboxImage) => void;
}) {
  if (urls.length === 0) {
    return null;
  }
  return (
    <div>
      <Box variant="h4" padding={{ bottom: 'xs' }}>
        {title}
      </Box>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
        {urls.map((url, index) => (
          <button
            key={index}
            type="button"
            onClick={() => onOpen({ url, title: `${title} ${index + 1}` })}
            aria-label={`View ${title.toLowerCase()} ${index + 1}`}
            style={{
              padding: 0,
              border: '1px solid #d5dbdb',
              borderRadius: '4px',
              background: 'none',
              cursor: 'pointer',
              lineHeight: 0,
            }}
          >
            <img
              src={url}
              alt={`${title} ${index + 1}`}
              style={{
                width: '72px',
                height: '72px',
                objectFit: 'cover',
                borderRadius: '3px',
              }}
            />
          </button>
        ))}
      </div>
    </div>
  );
}

export default function LabelerWorkspace() {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeJobId = searchParams.get('job');

  // Job list state (Requirements 7.1, 7.10).
  const [jobs, setJobs] = useState<LabelerJobSummary[]>([]);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState<string | null>(null);

  // Labeling view state.
  const [nextTask, setNextTask] = useState<LabelerNextTaskResponse | null>(
    null
  );
  const [taskLoading, setTaskLoading] = useState(false);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<LightboxImage | null>(null);

  const loadJobs = useCallback(async () => {
    setJobsLoading(true);
    setJobsError(null);
    try {
      const response = await apiService.getLabelerJobs();
      setJobs(response.jobs || []);
    } catch (err) {
      console.error('Failed to load labeler jobs:', err);
      setJobsError(getErrorMessage(err, 'Failed to load your labeling jobs'));
    } finally {
      setJobsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  /**
   * Fetch the labeler's next presentable Task_Assignment for the active
   * job (Requirement 7.1); the response also carries the fresh
   * submitted/remaining/withheld counts (Requirement 7.10) or the
   * completion payload (Requirement 7.11).
   */
  const loadNextTask = useCallback(async (jobId: string) => {
    setTaskLoading(true);
    setTaskError(null);
    setSubmitError(null);
    try {
      const response = await apiService.getNextTask(jobId);
      setNextTask(response);
    } catch (err) {
      console.error('Failed to load next task:', err);
      setTaskError(getErrorMessage(err, 'Failed to load your next task'));
    } finally {
      setTaskLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeJobId) {
      setNextTask(null);
      loadNextTask(activeJobId);
    } else {
      setNextTask(null);
    }
  }, [activeJobId, loadNextTask]);

  const enterJob = (jobId: string) => {
    setSearchParams({ job: jobId });
  };

  const backToJobs = () => {
    setSearchParams({});
    loadJobs();
  };

  /**
   * Submission flow (Requirements 7.7, 7.9, 7.10): persist the annotation,
   * then advance to the next task (whose payload refreshes the counts).
   * On failure the error alert is shown and the annotation stays on
   * screen — the canvas is not remounted because the task is unchanged.
   */
  const handleSubmit = async (annotation: DdaAnnotation) => {
    const taskId = nextTask?.task_id;
    if (!activeJobId || !taskId) {
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      await apiService.submitTask(taskId, activeJobId, annotation);
      await loadNextTask(activeJobId);
    } catch (err) {
      console.error('Failed to submit annotation:', err);
      // Incomplete-annotation rejections (Req 7.8) identify each missing
      // element in validation_errors, riding along on ApiError.details —
      // show those instead of only the generic header.
      const details = err instanceof ApiError ? err.details : undefined;
      const validationErrors = Array.isArray(details?.validation_errors)
        ? (details.validation_errors as Array<{ message?: string }>)
            .map((e) => e?.message)
            .filter((m): m is string => Boolean(m))
        : [];
      setSubmitError(
        validationErrors.length
          ? validationErrors.join(' • ')
          : getErrorMessage(
              err,
              'Your submission was not saved. Your annotation is still on screen — please try again.'
            )
      );
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * Presentation failure (Requirement 7.12): record the failure so the
   * task is withheld, then continue to the next presentable task. The
   * advance happens even if the report itself fails, so the labeler is
   * never stuck on an unpresentable image.
   */
  const handlePresentationFailure = async (reason: string) => {
    const taskId = nextTask?.task_id;
    if (!activeJobId || !taskId) {
      return;
    }
    try {
      await apiService.reportPresentationFailure(taskId, activeJobId, reason);
    } catch (err) {
      console.error('Failed to report presentation failure:', err);
    }
    await loadNextTask(activeJobId);
  };

  const activeJob = jobs.find((job) => job.job_id === activeJobId);

  // ---------------------------------------------------------------- //
  // Job list view (Requirements 7.1, 7.10)
  // ---------------------------------------------------------------- //
  if (!activeJobId) {
    return (
      <SpaceBetween size="l">
        <Header
          variant="h1"
          description="Labeling jobs in which you have images left to label."
        >
          My Labeling Tasks
        </Header>
        {jobsError && (
          <Alert
            type="error"
            header="Failed to load jobs"
            action={<Button onClick={loadJobs}>Retry</Button>}
          >
            {jobsError}
          </Alert>
        )}
        <Table
          columnDefinitions={[
            {
              id: 'job_name',
              header: 'Job',
              cell: (item: LabelerJobSummary) => item.job_name,
            },
            {
              id: 'task_type',
              header: 'Task type',
              cell: (item: LabelerJobSummary) => item.task_type,
            },
            {
              id: 'submitted',
              header: 'Submitted',
              cell: (item: LabelerJobSummary) => item.submitted_count,
            },
            {
              id: 'remaining',
              header: 'Remaining',
              cell: (item: LabelerJobSummary) => item.remaining_count,
            },
            {
              id: 'actions',
              header: '',
              cell: (item: LabelerJobSummary) => (
                <Button
                  variant="primary"
                  onClick={() => enterJob(item.job_id)}
                >
                  Start labeling
                </Button>
              ),
            },
          ]}
          items={jobs}
          loading={jobsLoading}
          loadingText="Loading your labeling jobs"
          empty={
            <Box textAlign="center" color="inherit" padding="l">
              <b>No labeling tasks</b>
              <Box variant="p" color="inherit">
                You have no images assigned for labeling right now.
              </Box>
            </Box>
          }
          header={<Header counter={`(${jobs.length})`}>Jobs</Header>}
        />
      </SpaceBetween>
    );
  }

  // ---------------------------------------------------------------- //
  // Labeling view
  // ---------------------------------------------------------------- //
  // Task fields are flat on the next-task payload (see
  // LabelerNextTaskResponse); a payload with a task_id carries a
  // presentable task, the completion payload carries none.
  const task =
    nextTask && !nextTask.complete && nextTask.task_id && nextTask.image_url
      ? {
          task_id: nextTask.task_id,
          image_url: nextTask.image_url,
          image_url_expires_at: nextTask.image_url_expires_at,
          prelabel: nextTask.prelabel,
        }
      : undefined;
  const submittedCount = nextTask?.submitted_count ?? 0;
  const remainingCount = nextTask?.remaining_count ?? 0;
  const withheldCount = nextTask?.withheld_count ?? 0;
  const goodExamples = nextTask?.example_images?.good ?? [];
  const badExamples = nextTask?.example_images?.bad ?? [];
  const instructions = nextTask?.instructions;
  const hasRail =
    Boolean(instructions) || goodExamples.length > 0 || badExamples.length > 0;

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={<Button onClick={backToJobs}>Back to jobs</Button>}
        description={
          nextTask ? (
            // Submitted/remaining counts, refreshed with every next-task
            // fetch (Requirement 7.10).
            <>
              Submitted: {submittedCount} · Remaining: {remainingCount}
              {withheldCount > 0 && <> · Withheld: {withheldCount}</>}
            </>
          ) : undefined
        }
      >
        {activeJob?.job_name ?? nextTask?.job_id ?? 'Labeling'}
      </Header>

      {taskError && (
        <Alert
          type="error"
          header="Failed to load task"
          action={
            <Button onClick={() => loadNextTask(activeJobId)}>Retry</Button>
          }
        >
          {taskError}
        </Alert>
      )}

      {taskLoading && !nextTask && (
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box variant="p">Loading your next task…</Box>
        </Box>
      )}

      {nextTask?.complete && (
        // Completion message with submitted and withheld counts
        // (Requirement 7.11).
        <Alert type="success" header="All done!">
          <SpaceBetween size="xs">
            <div>
              You have completed all your labeling tasks in this job. You
              submitted {submittedCount}{' '}
              {submittedCount === 1 ? 'image' : 'images'}.
            </div>
            {withheldCount > 0 && (
              <StatusIndicator type="warning">
                {withheldCount}{' '}
                {withheldCount === 1 ? 'task was' : 'tasks were'} withheld
                because the image could not be presented.
              </StatusIndicator>
            )}
            <Button onClick={backToJobs}>Back to jobs</Button>
          </SpaceBetween>
        </Alert>
      )}

      {task && !nextTask?.complete && (
        <>
          {submitError && (
            // Submission failure retains the annotation on screen
            // (Requirement 7.9): the canvas is not remounted.
            <Alert type="error" header="Submission not saved">
              {submitError}
            </Alert>
          )}
          <Grid
            gridDefinition={
              hasRail
                ? [
                    { colspan: { default: 12, m: 8, l: 9 } },
                    { colspan: { default: 12, m: 4, l: 3 } },
                  ]
                : [{ colspan: { default: 12 } }]
            }
          >
            {/* Remounted per task so annotation state resets between
                tasks (key={task_id}). */}
            <AnnotationCanvas
              key={task.task_id}
              imageUrl={task.image_url}
              imageUrlExpiresAt={task.image_url_expires_at}
              taskType={(nextTask?.task_type ?? '') as LabelingModality}
              labelSet={nextTask?.label_set ?? []}
              prelabel={task.prelabel}
              submitting={submitting}
              onSubmit={handleSubmit}
              onImageUrlRefresh={() =>
                apiService.refreshTaskImageUrl(task.task_id, activeJobId)
              }
              onPresentationFailure={handlePresentationFailure}
            />
            {hasRail && (
              // Right rail: instructions and good/bad examples on the
              // same screen as the image, absent items omitted
              // (Requirement 7.2).
              <Container header={<Header variant="h3">Job guidance</Header>}>
                <SpaceBetween size="m">
                  {instructions && (
                    <div>
                      <Box variant="h4" padding={{ bottom: 'xs' }}>
                        Instructions
                      </Box>
                      <Box variant="p">
                        <span style={{ whiteSpace: 'pre-wrap' }}>
                          {instructions}
                        </span>
                      </Box>
                    </div>
                  )}
                  <ExampleThumbnails
                    title="Good examples"
                    urls={goodExamples}
                    onOpen={setLightbox}
                  />
                  <ExampleThumbnails
                    title="Bad examples"
                    urls={badExamples}
                    onOpen={setLightbox}
                  />
                </SpaceBetween>
              </Container>
            )}
          </Grid>
        </>
      )}

      {/* Simple lightbox for example images (Requirement 7.2). */}
      <Modal
        visible={lightbox !== null}
        onDismiss={() => setLightbox(null)}
        header={lightbox?.title}
        size="large"
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setLightbox(null)}>
              Close
            </Button>
          </Box>
        }
      >
        {lightbox && (
          <img
            src={lightbox.url}
            alt={lightbox.title}
            style={{ maxWidth: '100%', maxHeight: '70vh', display: 'block', margin: '0 auto' }}
          />
        )}
      </Modal>
    </SpaceBetween>
  );
}
