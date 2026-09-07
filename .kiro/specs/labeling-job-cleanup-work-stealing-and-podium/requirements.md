# Requirements Document

## Introduction

Four user-requested quality-of-life features for the DDA labeling domain, verbatim:

1. *"I would like a way to delete and clean up old labeling jobs."*
2. *"I would like the ability of 1 team member labeling finished before the other, they can take the other team members work until there is nothing left."* (work stealing)
3. *"Add gamification when the job is done, show a winner podium with who is 1st, 2nd, and 3rd place."*
4. *"a 'clear button' and clears away any pre-labeled regions in case their are entirely incorrect."* (in the labeling canvas)

What exists to build on, verified in the shipped code:

- **The labeling domain is portal-native and conditional-write disciplined.** `dda_labeling.py` owns the jobs table (`dda-portal-labeling-jobs`, PK `job_id`, GSI `usecase-jobs-index`), the tasks table (`dda-portal-labeling-tasks`, PK `job_id` / SK `task_id`, GSI `assignee-index` on `assignee_user_id`), and the single-table teams store (`dda-portal-labeling-teams`, `META` / `MEMBER#<sub>` sort keys). Task statuses: `Assigned`, `Submitted`, `PresentationFailed`, `Inactive`; job statuses: `InProgress`, `Completed`, `Failed`, `Stopped`. Every task mutation is a conditional write: `_conditional_reassign` moves a task iff `status = Assigned AND assignee_user_id = :from`; `submit_labeler_task` submits iff `status = Assigned AND assignee_user_id = :caller`. Two sentinels exist: `UNASSIGNED` (tasks parked when a team's last member is removed) and `AUTO` (skip-verification result items).
- **The async worker action pattern is the deletion vehicle.** `_invoke_labeling_worker(payload)` fire-and-forget-invokes `DdaLabelingWorker` (`dda_labeling_worker.py`), whose handler dispatches on `payload['action']`: `distribute`, `notify_new_members`, `generate_manifest`, `retry_prelabels`. Long-running job mutations run there, never in the API request.
- **The job's own S3 artifacts live under one prefix.** Pre-labels are written to `labeling/{usecase_id}/{job_id}/prelabels/{task_id}.json` and Segmentation annotations to `labeling/{usecase_id}/{job_id}/annotations/{task_id}.json`, both in the portal artifacts bucket (`PORTAL_ARTIFACTS_BUCKET`, `dda-portal-artifacts-*`). The dataset images live in the Use_Case's own bucket (`dataset_bucket`/`dataset_prefix`, e.g. `s3://ryvan-cookies/training-images/`) and are read-only inputs. Output manifests and masks go to the Use_Case output bucket under `labeled/{job_id}/` and are consumed by training.
- **Submission already records everything a podium needs.** Each submitted task carries `submitted_by` (the labeler's sub), `submitted_at` (epoch), and `submitted_at_iso`; the job detail route (`labeling.py::_get_dda_labeling_job`) already aggregates per-member `member_progress` from a full task query, and the shared layer (`labeling_distribution.py`) is the domain's home for deterministic pure functions.
- **The canvas knows which geometry came from the Pre_Label.** `AnnotationCanvas.tsx` initializes ObjectDetection boxes with ids `prelabel-box-{i}` (user-drawn boxes get `box-{timestamp}-{n}`), paints Segmentation Pre_Label regions into the label-indexed bitmap at image load, and holds classless SAM proposals as `proposal-{i}` items. Classification pre-selects `prelabel.label`. The component is remounted per task, so per-task state is structural.
- **New routes ride the DDA labeling nested stack.** `dda-labeling-api-stack.ts` imports the portal Rest API and `/labeling/{id}` by resource id, adds Cognito-authorized methods via one `addMethod` helper, and rolls a fresh stage deployment whose logical id is salted with the route table — new routes deploy by construction.

Scoping decisions, each with its rationale:

- **Deletion requires a resting job: `Completed`, `Failed`, `Stopped`, or `DeleteFailed` — never `InProgress`.** "Old labeling jobs" are finished ones. An InProgress job has labelers actively submitting into it; the shipped stop flow is the way to end work, and delete composes with it (stop, then delete). The delete route names the stop-first path in its rejection.
- **Deletion is an async worker action with a `Deleting` status, mirroring the `distribute` pattern.** A job can hold thousands of task items and artifact objects; the API answers 202 after the conditional `→ Deleting` transition and the worker does the walking. The job record is deleted **last**, so a crashed deletion stays visible as `Deleting`, and a repeated DELETE re-triggers the (idempotent) worker action rather than erroring — self-healing by construction.
- **The dataset prefix is sacrosanct; the output manifest is retained.** Cleanup deletes exactly the job's task items and the objects under the job's own `labeling/{usecase_id}/{job_id}/` prefix in the portal artifacts bucket. It never issues a delete against the Use_Case dataset bucket (the user's raw images), and it deliberately retains `labeled/{job_id}/` in the Use_Case output bucket — a Completed job's manifest may back registered training datasets, and severing that silently is worse than leaving a small deliverable behind.
- **Ground Truth jobs are not deletable through this route.** Their artifacts and lifecycle are SageMaker-managed (the stop route draws the same line). The DDA cleanup semantics (task items, portal-artifact prefixes) do not exist for them.
- **Work stealing transfers one task per request through the existing conditional-write discipline.** The steal write's condition (`status = Assigned AND assignee_user_id = :donor`) is exactly the `_conditional_reassign` semantics, so two stealers can never both take one task and a concurrent submission always wins — the submit guard falls out of the condition rather than being bolted on. `UNASSIGNED` tasks are offered first (they have no owner to inconvenience), then tasks from the most-loaded teammate, all deterministically ordered. Tasks whose Pre_Label is still `Pending` are not stealable — stealing one would hand the stealer a withheld task; `AUTO` items are structurally unreachable (skip-verification jobs have no team) and excluded by predicate anyway.
- **The steal offer and the podium ride a new labeler pool route instead of the completion payload.** `test_dda_labeling_labeler_apis.py` pins the `GET /labeler/jobs/{jobId}/next` completion payload with exact-dict equality. Adding keys there would force a test rebaseline for zero functional gain; a sibling `GET /labeler/jobs/{jobId}/pool` route carries `{stealable_count, job_complete, podium}` and leaves every shipped payload byte-identical.
- **Podium ranking: submitted-task count descending, ties broken by earliest final submission.** The labeler whose last submission landed earliest finished their volume first. A final `user_id` tie-break makes the order total, so the ranking is a deterministic pure function — it lives in the shared layer (`labeling_distribution.py`, beside `distribute`/`rebalance`) with `labeling.py` (admin detail) and `dda_labeling.py` (labeler pool) as its two consumers. Skip-verification and Ground Truth jobs have no team and no podium. No gamification beyond the podium (explicit scope guard).
- **Clear-prelabels is entirely client-side.** The stored Pre_Label artifact and task item are never touched — the control clears the canvas's prelabel-derived state (provenance-tagged boxes, the Pre_Label's still-intact painted pixels, classless proposals, the pre-selected classification) while retaining the labeler's own work, and offers a one-level restore (session-scoped undo snapshot). Submission then persists whatever is on the canvas through the existing validation, unchanged.
- **Zero test rebaseline.** Every pre-existing test stays byte-identical: the pinned completion payload is untouched, the DDA job detail payload gains only an additive `podium` key (its tests pin individual entries, not the whole dict), the new UI renders only in states existing fixtures do not enter (delete controls only for resting jobs; podium only for Completed jobs; clear control only when a Pre_Label is present — existing canvas tests are helpers-only). If any pre-existing assertion has to change, that is a design violation to stop on.

## Glossary

Terms carried over from the dda-data-labeling, llm-autolabel-prompt-tuning, grounded-sam-autolabel, grounded-sam-prompt-guardrails-and-prelabel-retry, and grounded-sam-prompt-tuning-preview specs keep their existing definitions (Portal, DDA_Labeling_System, Labeling_Job, Labeling_Team, Data_Labeler, Task_Assignment, Pre_Label, Labeler_Interface, Annotation_Canvas, Use_Case, Job_Creator, Auto_Labeler, Admin_Review, Skip_Verification_Mode, Prompt_Tuning_Preview). New or constrained terms:

- **Deletion_Route**: `DELETE /labeling/{id}` on `DdaLabelingHandler`, authorized by `MANAGE_LABELING_JOBS` through `rbac_check` in the job's Use_Case scope (the stop-route pattern).
- **Deletable_Status**: the job statuses from which a deletion may be requested: `Completed`, `Failed`, `Stopped`, `DeleteFailed`, and `Deleting` (the last as an idempotent re-trigger of a stalled deletion). `InProgress` is excluded — stop first.
- **Deleting**: the new job status a job holds from the accepted deletion request until its record is removed or the cleanup fails.
- **Delete_Failed**: the new job status (stored value `DeleteFailed`) recorded with a `failure_reason` when the Deletion_Worker cannot complete; a job in this status may be re-deleted.
- **Deletion_Worker**: the `{action: 'delete_job', job_id}` arm of `DdaLabelingWorker` (`dda_labeling_worker.py`), async-invoked by the Deletion_Route.
- **Job_Artifact_Prefix**: `labeling/{usecase_id}/{job_id}/` in the Portal_Artifacts_Bucket — the prefix holding the job's pre-labels and Segmentation annotations.
- **Portal_Artifacts_Bucket**: the bucket named by `PORTAL_ARTIFACTS_BUCKET` (`dda-portal-artifacts-*`).
- **Dataset_Location**: the job's `dataset_bucket` and `dataset_prefix` — the Use_Case's raw images, sacrosanct to every cleanup path.
- **Steal_Route**: `POST /labeler/jobs/{jobId}/steal` on `DdaLabelingHandler`, authorized like every labeler route (`LABELING_TASKS_SELF` plus the server-side current-team-membership check).
- **Steal_Request**: one invocation of the Steal_Route, transferring at most one Task_Assignment to the caller.
- **Stealable_Task**: a Task_Assignment of the named job with `status = Assigned`, an assignee that is neither the caller nor `AUTO`, and a `prelabel_status` other than `Pending`. `UNASSIGNED` tasks are Stealable_Tasks.
- **Donor**: the assignee a stolen task is taken from (a teammate's sub or the `UNASSIGNED` sentinel).
- **Steal_Order**: the deterministic candidate order of a Steal_Request: `UNASSIGNED` tasks first in ascending `task_id`; then Donors by descending Stealable_Task count, ties in ascending user id, and within one Donor ascending `task_id`.
- **Pool_Route**: `GET /labeler/jobs/{jobId}/pool` on `DdaLabelingHandler`, same authorization posture as the Steal_Route, reporting the job's team-pool state to the caller.
- **Job_Complete**: the condition that every one of the job's active (non-`Inactive`) Task_Assignments is `Submitted` and the job's `image_count` is positive — the same condition that triggers manifest generation.
- **Podium_Ranking**: the deterministic ranking of a job's submitters: descending Submitted-task count, ties by ascending Final_Submission_Timestamp, residual ties by ascending user id; implemented as one shared pure function in `labeling_distribution.py`.
- **Final_Submission_Timestamp**: for one submitter within one job, the maximum `submitted_at` among that submitter's Submitted tasks.
- **Podium_Entry**: one ranked entry: `{place (1|2|3), user_id, email?, submitted, final_submitted_at}`, `email` joined from the team store when the submitter is a current member.
- **Winner_Podium**: the celebration rendering of up to three Podium_Entries (`WinnerPodium.tsx`), 1st place most prominent, shown on the admin job detail page and in the Labeler_Interface completion view.
- **Clear_Prelabels_Control**: the Annotation_Canvas control (testid `clear-prelabels`) that removes the presented task's prelabel-derived annotation state.
- **Prelabel_Origin**: annotation state initialized from the task's Pre_Label: ObjectDetection boxes with `prelabel-box-` ids, the Segmentation pixels painted from the Pre_Label's regions at initialization, classless `proposal-` items, and the pre-selected Classification label.
- **Prelabel_Snapshot**: the in-memory copy of the full annotation state taken at the moment the Clear_Prelabels_Control is activated, backing the restore.
- **Restore_Control**: the control (testid `restore-prelabels`) the Clear_Prelabels_Control becomes after clearing; activating it reinstates the Prelabel_Snapshot exactly (one-level, session-scoped undo).

## Requirements

### Requirement 1: Deletion request — status-gated, admin-authorized, async

**User Story:** As a Job_Creator, I want to delete an old labeling job, so that finished and failed jobs stop cluttering the portal and their storage is reclaimed.

#### Acceptance Criteria

1. WHEN the Deletion_Route receives a request naming an existing DDA Labeling_Job whose status is a Deletable_Status, THE DDA_Labeling_System SHALL transition the job to Deleting with a conditional write on the read status, record `delete_requested_by` and `delete_requested_at`, async-invoke the Deletion_Worker with `{action: 'delete_job', job_id}`, and answer 202 with `{job_id, status: 'Deleting'}`.
2. IF the Deletion_Route request names a job that does not exist, THEN THE DDA_Labeling_System SHALL answer 404 with nothing changed.
3. IF the Deletion_Route request names a Ground Truth job, THEN THE DDA_Labeling_System SHALL answer 400 with a validation error stating that Ground Truth job lifecycles are SageMaker-managed, with nothing changed.
4. IF the Deletion_Route request names a DDA job whose status is InProgress, THEN THE DDA_Labeling_System SHALL answer 400 with a validation error naming the current status and stating the job must be stopped before deletion, with nothing changed.
5. WHEN the Deletion_Route receives a request naming a job already in the Deleting status, THE DDA_Labeling_System SHALL re-invoke the Deletion_Worker and answer 202 — the idempotent recovery path for a stalled deletion.
6. THE Deletion_Route SHALL authorize each request with `MANAGE_LABELING_JOBS` through `rbac_check` in the job's Use_Case scope, with the job's `usecase_id` injected the way the stop route injects it.
7. WHEN a deletion request is accepted, THE DDA_Labeling_System SHALL write a `job_delete_requested` audit event carrying the acting user, the job id, and the prior status.
8. IF the conditional Deleting transition fails because the status changed concurrently, THEN THE DDA_Labeling_System SHALL re-read the job and answer according to the new status with no partial write.

### Requirement 2: Deletion cleanup — the Deletion_Worker action

**User Story:** As a portal operator, I want the deletion to actually reclaim the job's storage, so that deleted jobs leave no task items or artifact objects behind.

#### Acceptance Criteria

1. WHEN the Deletion_Worker runs for a job in the Deleting status, THE Deletion_Worker SHALL delete every object under the job's Job_Artifact_Prefix in the Portal_Artifacts_Bucket, using paginated listing and batched deletes.
2. WHEN the artifact deletion completes, THE Deletion_Worker SHALL delete every task item of the job from the tasks table.
3. WHEN the task-item deletion completes, THE Deletion_Worker SHALL delete the job record itself.
4. THE Deletion_Worker SHALL delete the job record only after the artifact and task-item deletions have completed, so an interrupted deletion remains visible as a Deleting job.
5. IF the Deletion_Worker is invoked for a job that does not exist or whose status is not Deleting, THEN THE Deletion_Worker SHALL record the invocation as skipped and perform zero deletions.
6. IF any cleanup step fails, THEN THE Deletion_Worker SHALL set the job status to Delete_Failed with a recorded `failure_reason`, leaving already-deleted items deleted.
7. WHEN the cleanup completes, THE Deletion_Worker SHALL write a `job_deleted` audit event carrying the job id, the Use_Case, and the deleted task-item and artifact-object counts.
8. WHEN the Deletion_Worker runs again for the same Deleting job after an interruption or a Delete_Failed retry, THE Deletion_Worker SHALL complete the remaining deletions — every cleanup step SHALL be idempotent.

### Requirement 3: Deletion safety — the dataset is sacrosanct

**User Story:** As a Use_Case owner, I want job deletion to be provably unable to touch my raw images or training deliverables, so that cleanup can never destroy data I cannot regenerate.

#### Acceptance Criteria

1. THE Deletion_Worker SHALL issue no delete or write operation against the job's Dataset_Location.
2. THE Deletion_Worker SHALL confine every S3 deletion to Portal_Artifacts_Bucket keys beginning with the job's own Job_Artifact_Prefix.
3. THE Deletion_Worker SHALL retain the job's output manifest and masks under the Use_Case output bucket's `labeled/{job_id}/` prefix.
4. WHEN a job is deleted, THE Deletion_Worker SHALL leave every other job's task items and Job_Artifact_Prefix objects untouched.
5. THE Deletion_Worker SHALL leave every object under the `labeling-previews/` prefix untouched.

### Requirement 4: Deletion surfaces in the Portal

**User Story:** As a Job_Creator, I want delete controls on the job list and job detail pages with a clear statement of what is removed, so that cleanup is one confirmed click and never a surprise.

#### Acceptance Criteria

1. WHILE a DDA job's status is Completed, Failed, Stopped, or Delete_Failed, THE Portal SHALL offer a delete control for that job on the job detail page and for the selected job on the labeling jobs list page.
2. WHILE a DDA job's status is InProgress or Deleting, THE Portal SHALL offer no delete control for that job.
3. WHEN a delete control is activated, THE Portal SHALL present a confirmation dialog naming the job and stating that its task assignments and pre-label/annotation artifacts are removed while the dataset images and any generated training manifest are retained.
4. WHEN the confirmation is accepted, THE Portal SHALL issue the Deletion_Route request and, on the 202 answer, render the job in the Deleting status.
5. IF the Deletion_Route request fails, THEN THE Portal SHALL surface the error message with the job rendered unchanged.
6. THE Portal SHALL render the Deleting and Delete_Failed statuses distinctly on the job list and job detail pages, and a Delete_Failed job's delete control SHALL read as a retry.
7. WHEN a deletion completes and the job record no longer exists, THE Portal SHALL no longer list the job.

### Requirement 5: Work stealing — the Steal_Route

**User Story:** As a Data_Labeler who finished my assigned images, I want to take unfinished work from my teammates one task at a time, so that the team finishes the job as fast as possible.

#### Acceptance Criteria

1. WHEN the Steal_Route receives a request for an InProgress DDA team job from a current member of the job's Labeling_Team and at least one Stealable_Task exists, THE DDA_Labeling_System SHALL reassign exactly one Stealable_Task to the caller and answer 200 with `{task_id, job_id, stolen_from, stealable_count}` (the count of Stealable_Tasks remaining after the transfer).
2. THE DDA_Labeling_System SHALL choose the transferred task by the Steal_Order, so the same task population always yields the same choice.
3. THE DDA_Labeling_System SHALL apply each transfer as a conditional write requiring `status = Assigned AND assignee_user_id = :donor`, and WHEN the condition fails because of a concurrent submission, steal, or reassignment, THE DDA_Labeling_System SHALL proceed to the next candidate in Steal_Order — two concurrent Steal_Requests SHALL never both acquire the same task.
4. THE DDA_Labeling_System SHALL never reassign a Submitted, PresentationFailed, or Inactive task, a task whose `prelabel_status` is Pending, or an `AUTO` result item through the Steal_Route.
5. WHEN zero Stealable_Tasks exist or every candidate is lost to concurrent writes, THE Steal_Route SHALL answer 409 stating no stealable tasks remain, with no assignment changed.
6. IF the named job is not InProgress, THEN THE Steal_Route SHALL answer 409 naming the job's status, with no assignment changed.
7. THE Steal_Route SHALL deny callers who are not current members of the job's Labeling_Team — and requests naming a missing or Ground Truth job — with the labeler-route 403 carrying no resource data plus a `labeler_access_denied` audit event.
8. WHEN a task is transferred, THE DDA_Labeling_System SHALL record `stolen_from` and `stolen_at` on the task item and write a `task_stolen` audit event carrying the caller, the Donor, the task id, and the job id.
9. WHEN a task has been transferred to the caller, THE DDA_Labeling_System SHALL serve it through the existing next-task flow as the caller's own Task_Assignment.

### Requirement 6: Work stealing — the Labeler_Interface offer

**User Story:** As a Data_Labeler on the completion screen, I want to see how much teammate work is left and take it with one action, so that "until there is nothing left" is a loop I can actually drive.

#### Acceptance Criteria

1. WHEN the Pool_Route receives a request for a DDA team job from a current member of the job's Labeling_Team, THE DDA_Labeling_System SHALL answer 200 with `{job_id, stealable_count, job_complete}` where `stealable_count` is the exact count of Stealable_Tasks for the caller while the job is InProgress and zero otherwise, and `job_complete` reflects the Job_Complete condition, plus a `podium` list exactly when Job_Complete holds; non-members and requests naming a missing or Ground Truth job SHALL be denied with the labeler-route 403 plus audit event.
2. WHEN the Labeler_Interface reaches the completion state for a job and the Pool_Route reports `stealable_count > 0`, THE Labeler_Interface SHALL offer a take-work control stating how many teammate images remain.
3. WHEN the take-work control is activated, THE Labeler_Interface SHALL issue one Steal_Request and, on success, SHALL load and present the caller's next task through the existing flow.
4. WHEN the labeler completes a stolen task and reaches the completion state again while Stealable_Tasks remain, THE Labeler_Interface SHALL offer the take-work control again — repeating until nothing is left.
5. IF a Steal_Request answers that no stealable tasks remain, THEN THE Labeler_Interface SHALL refresh the pool state and render the completion view without an error indication.
6. WHILE the Pool_Route reports `stealable_count = 0` and Job_Complete false, THE Labeler_Interface SHALL render the existing completion message unchanged.

### Requirement 7: Podium_Ranking — deterministic, shared, tie-broken by finish time

**User Story:** As a portal operator, I want one deterministic ranking computation shared by every podium surface, so that both surfaces always agree on who won.

#### Acceptance Criteria

1. THE Podium_Ranking SHALL rank a job's submitters by descending count of Submitted tasks attributed to them via `submitted_by`.
2. WHEN two submitters hold equal Submitted counts, THE Podium_Ranking SHALL rank the submitter with the earlier Final_Submission_Timestamp first.
3. WHEN submitters remain tied on count and Final_Submission_Timestamp, THE Podium_Ranking SHALL order them by ascending user id, making the ranking total and deterministic.
4. THE Podium_Ranking SHALL emit at most three Podium_Entries with places 1, 2, and 3 in rank order, emitting fewer entries when fewer submitters exist and an empty list when none exist.
5. WHEN a Podium_Entry's submitter is a current member of the job's Labeling_Team, THE DDA_Labeling_System SHALL carry the member's email on the entry, and otherwise SHALL carry the user id alone.
6. THE Podium_Ranking SHALL be implemented as one shared pure function in the labeling distribution shared-layer module, consumed by both the job detail payload and the Pool_Route.

### Requirement 8: Winner_Podium surfaces

**User Story:** As a Data_Labeler and as a Job_Creator, I want to see the 1st/2nd/3rd podium when the job is done, so that finishing a labeling job feels like winning something.

#### Acceptance Criteria

1. WHEN the job detail page shows a DDA team job in the Completed status, THE Portal SHALL render the Winner_Podium from the detail payload's podium entries.
2. WHEN the DDA job detail payload is assembled for a Completed team job, THE DDA_Labeling_System SHALL include the `podium` list computed by the Podium_Ranking, and SHALL omit the key for every other job (non-team, non-Completed, Ground Truth, or Skip_Verification_Mode).
3. WHEN the Labeler_Interface completion view finds Job_Complete true with a non-empty podium from the Pool_Route, THE Labeler_Interface SHALL render the Winner_Podium in place of the take-work offer.
4. THE Winner_Podium SHALL render 1st place most prominently with 2nd and 3rd beside it, each Podium_Entry showing its place, its display name (email when carried, user id otherwise), and its Submitted count.
5. WHILE a job's podium data is absent or empty, THE Portal SHALL render no Winner_Podium for that job.

### Requirement 9: Clear pre-labels in the Annotation_Canvas

**User Story:** As a Data_Labeler facing an entirely wrong Pre_Label, I want one control that clears the pre-labeled regions without losing my own work, and a way to undo the clear, so that a bad Pre_Label costs one click instead of many.

#### Acceptance Criteria

1. WHILE the presented task carries a non-empty Pre_Label (a classification selection, at least one box, or at least one region), THE Annotation_Canvas SHALL offer the Clear_Prelabels_Control; tasks without a Pre_Label SHALL offer no such control.
2. WHEN the Clear_Prelabels_Control is activated on an ObjectDetection task, THE Annotation_Canvas SHALL remove exactly the boxes of Prelabel_Origin — including ones whose class the labeler edited — and SHALL retain every user-drawn box.
3. WHEN the Clear_Prelabels_Control is activated on a Segmentation task, THE Annotation_Canvas SHALL clear each painted pixel that still holds the class the Pre_Label initialized it to, SHALL remove the remaining classless proposals, and SHALL retain every pixel the labeler painted or repainted.
4. WHEN the Clear_Prelabels_Control is activated on a Classification task, THE Annotation_Canvas SHALL deselect the selection when it still equals the Pre_Label's label and SHALL retain a selection the labeler changed.
5. WHEN the Clear_Prelabels_Control is activated, THE Annotation_Canvas SHALL take the Prelabel_Snapshot first and SHALL replace the control with the Restore_Control; WHEN the Restore_Control is activated, THE Annotation_Canvas SHALL reinstate the Prelabel_Snapshot exactly and SHALL offer the Clear_Prelabels_Control again.
6. THE Clear_Prelabels_Control SHALL affect only the presented task's on-canvas state: the stored Pre_Label artifact, the task item, and every other task SHALL be unchanged by clearing or restoring.
7. WHEN a submission follows a clear or restore, THE Labeler_Interface SHALL submit the on-canvas annotation state through the existing completeness validation unchanged.
8. WHEN the presigned image URL is refreshed after a clear, THE Annotation_Canvas SHALL preserve the cleared state and the Restore_Control exactly as annotation state is already preserved across URL swaps.

### Requirement 10: Preservation and zero rebaseline

**User Story:** As a portal operator, I want everything outside these four features to behave byte-identically, so that cleanup, stealing, podium, and clear cannot regress the shipped labeling pipeline.

#### Acceptance Criteria

1. THE DDA_Labeling_System SHALL leave the `GET /labeler/jobs/{jobId}/next` completion payload, the submit flow's conditional write and counter, the stop route, the Admin_Review routes, the rerun-prelabels route, and the Prompt_Tuning_Preview machinery byte-identical to before this feature.
2. THE DDA_Labeling_System SHALL leave `_conditional_reassign` and every membership-change reassignment path unchanged; the Steal_Route SHALL apply its own conditional write without modifying those functions.
3. THE DDA_Labeling_System SHALL leave `dda_autolabel_worker.py`'s generation logic, the `grounded-sam-worker/` image, and every Auto_Labeler code path unchanged by this feature.
4. WHEN the DDA job detail payload is assembled for a non-Completed or non-team job, THE DDA_Labeling_System SHALL produce a payload byte-identical to before this feature — the podium key is strictly additive.
5. WHEN this feature's changes land, THE test suite SHALL require zero amendments to pre-existing test files; any pre-existing assertion that has to change SHALL be treated as a design violation to stop on.
6. WHILE a job is not being deleted, THE Deletion_Worker's existence SHALL have no effect on that job: distribution, submission, manifest generation, and review operate unchanged.
