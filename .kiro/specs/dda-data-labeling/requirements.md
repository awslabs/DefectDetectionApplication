# Requirements Document

## Introduction

The DDA Data Labeling System is a portal-native replacement for SageMaker Ground Truth. Today, labeling jobs created from the portal (edge-cv-portal) are delegated to SageMaker Ground Truth, which requires SageMaker work teams, a separate worker portal, and post-hoc manifest transformation into the DDA format consumed by training and compilation jobs. This feature adds a first-class labeling backend inside the portal itself: private labeling teams whose members hold a restricted Data Labeler role, a labeler web interface with per-image instructions and examples, automatic work distribution across team members, optional model-assisted pre-labeling (Segment Anything and other open-source models, or Amazon Bedrock models), an admin-only skip-verification mode driven by a Bedrock LLM, SES email notifications, and — critically — direct emission of the exact DDA augmented manifest format (`source-ref`, `anomaly-label`, `anomaly-label-metadata`, `anomaly-mask-ref`, `anomaly-mask-ref-metadata`, and the bounding-box attribute for object detection) so completed jobs feed existing training and compilation flows with no conversion step.

The portal remains an AWS serverless application (Lambda, API Gateway, DynamoDB, S3, Cognito, CloudFront); this feature uses AWS services throughout (SES for email, Cognito for labeler identity, Bedrock for LLM auto-labeling, S3 for images/masks/manifests). SageMaker Ground Truth remains available as an alternative backend, selected at job creation time. This spec is self-contained and additive to the existing portal source tree.

## Glossary

- **DDA_Labeling_System**: The new portal-native data labeling backend introduced by this feature, comprising the job manager, task distributor, labeler interface, auto-labeler, and manifest generator.
- **Portal**: The existing edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **Labeling_Job**: A unit of labeling work created by an authorized portal user against a dataset prefix in S3, with a chosen backend, modality, label set, instructions, and example images.
- **Labeling_Backend**: The engine that executes a Labeling_Job — either the DDA_Labeling_System or SageMaker Ground Truth.
- **Labeling_Team**: A named, private group of portal users with the Data_Labeler role, associated with a Use_Case, to which DDA Labeling_Jobs are assigned.
- **Data_Labeler**: A new portal role whose members can access only the Labeler_Interface and the labeling APIs required by it, and no other portal pages or APIs.
- **Labeler_Interface**: The portal page where a Data_Labeler views assigned images one at a time, sees job instructions and example images, and submits labels.
- **Task_Assignment**: The mapping of a single dataset image within a Labeling_Job to exactly one Data_Labeler for labeling.
- **Labeling_Modality**: The kind of annotation a Labeling_Job collects — Binary_Classification, Semantic_Segmentation, or Object_Detection.
- **Binary_Classification**: A Labeling_Modality where each image is labeled normal or anomaly.
- **Semantic_Segmentation**: A Labeling_Modality where the labeler paints pixel masks whose classes are chosen from the Label_Set defined at job creation (as consumed by RF-DETR-style segmentation training).
- **Object_Detection**: A Labeling_Modality where the labeler draws bounding boxes with classes chosen from the Label_Set (as consumed by YOLO-style training).
- **Label_Set**: The ordered list of class names defined at Labeling_Job creation, used for Semantic_Segmentation and Object_Detection.
- **Auto_Labeler**: The DDA_Labeling_System component that generates Pre_Labels for images using a model selected at job creation — a Segment Anything (SAM) model, another supported open-source model, or an Amazon Bedrock model.
- **Pre_Label**: A machine-generated candidate annotation attached to a Task_Assignment, pending human approval or correction.
- **Skip_Verification_Mode**: A job option, restricted to admins, in which the Auto_Labeler labels every image using a selected Bedrock model with Per_Label_Prompts, no Task_Assignments are created, and the admin reviews all results at the end.
- **Per_Label_Prompt**: An admin-authored prompt associated with one label in the Label_Set, sent to the Bedrock model in Skip_Verification_Mode to guide auto-labeling for that label.
- **Admin_Review**: The portal page where, after Skip_Verification_Mode auto-labeling completes, the job creator inspects each auto-labeled result and individually accepts or rejects it for inclusion in the output manifest.
- **DDA_Manifest**: The JSON Lines augmented manifest format consumed by the Portal's existing training and compilation jobs: one JSON object per image containing `source-ref` plus the modality's label attributes (`anomaly-label` and `anomaly-label-metadata` for classification; additionally `anomaly-mask-ref` and `anomaly-mask-ref-metadata` for segmentation; a bounding-box label attribute and its `-metadata` companion for object detection).
- **Manifest_Generator**: The DDA_Labeling_System component that serializes approved labels into a DDA_Manifest and writes it to S3.
- **Notification_Service**: The DDA_Labeling_System component that sends email to Labeling_Team members via Amazon SES.
- **Use_Case**: An existing portal tenant construct; datasets, jobs, and teams are scoped to a Use_Case.
- **Job_Creator**: The portal user (with labeling-job creation permission, e.g., DataScientist, UseCaseAdmin, or PortalAdmin) who creates a Labeling_Job.

## Requirements

### Requirement 1: Labeling Backend Selection

**User Story:** As a Job_Creator, I want to choose between the DDA Data Labeling System and SageMaker Ground Truth when creating a labeling job, so that I can use the portal-native workflow or the existing SageMaker workflow as appropriate.

#### Acceptance Criteria

1. WHEN a Job_Creator opens the labeling job creation form, THE Portal SHALL present a Labeling_Backend choice offering exactly two options, DDA_Labeling_System and SageMaker Ground Truth, and SHALL require a selection before the form can be submitted.
2. WHEN a Job_Creator submits a Labeling_Job with the SageMaker Ground Truth backend, THE Portal SHALL create the job through the existing SageMaker Ground Truth flow, accepting the same job parameters and producing a SageMaker labeling job as it did before this feature, and SHALL invoke no DDA_Labeling_System component for that job.
3. WHEN a Job_Creator submits a Labeling_Job with the DDA_Labeling_System backend, THE DDA_Labeling_System SHALL create the job entirely within portal-managed AWS resources and SHALL create no SageMaker labeling job and no SageMaker work team for that job.
4. WHEN a Labeling_Job is created with either Labeling_Backend, THE Portal SHALL persist the selected Labeling_Backend with the job record.
5. WHEN listing labeling jobs for a Use_Case, THE Portal SHALL include jobs from both Labeling_Backends in a single job list, and SHALL include with each listed job its persisted Labeling_Backend value distinguishing DDA_Labeling_System jobs from SageMaker Ground Truth jobs.
6. IF a Labeling_Job submission omits the Labeling_Backend or specifies a Labeling_Backend value other than DDA_Labeling_System or SageMaker Ground Truth, THEN THE Portal SHALL reject the submission with a validation error indicating the invalid backend value and SHALL create no Labeling_Job and no backend resources.

### Requirement 2: Data Labeler Role and Access Restriction

**User Story:** As a portal administrator, I want labeler users to hold a dedicated Data Labeler role that grants access only to the labeling interface, so that external labelers can work on labeling tasks without seeing any other portal functionality.

#### Acceptance Criteria

1. THE Portal SHALL support a Data_Labeler role that authorized administrators can assign to and revoke from portal users through the existing user administration functions, with each assignment or revocation enforced on the user's next authenticated request.
2. WHEN a user whose only role is Data_Labeler signs in, THE Portal SHALL route the user directly to the Labeler_Interface and SHALL hide all navigation destinations other than the Labeler_Interface, sign out, and account settings.
3. IF a user whose only role is Data_Labeler requests a backend API outside the labeling APIs required by the Labeler_Interface, THEN THE Portal SHALL reject the request with an authorization error containing no portal data, and SHALL record an audit event containing the requesting user's identity, the requested API, and a timestamp.
4. WHEN a Data_Labeler requests labeling data, THE Portal SHALL return only Task_Assignments assigned to that Data_Labeler within jobs assigned to a Labeling_Team of which the Data_Labeler is a current member, and SHALL return an empty result when no such Task_Assignments exist.
5. THE Portal SHALL authenticate Data_Labeler users through the existing Amazon Cognito user pool.
6. IF a Data_Labeler requests a Task_Assignment, dataset image, or Labeling_Job that is not assigned to that Data_Labeler under criterion 4, THEN THE Portal SHALL reject the request with an authorization error containing none of the requested data and SHALL record an audit event.
7. IF a user whose only role is Data_Labeler navigates directly to a Portal page URL other than the Labeler_Interface, sign out, or account settings, THEN THE Portal SHALL redirect the user to the Labeler_Interface without rendering the requested page.
8. WHILE a user holds the Data_Labeler role together with at least one other portal role, THE Portal SHALL apply the navigation and API permissions granted by the user's other roles unchanged, and SHALL NOT apply the Data_Labeler-only navigation and API restrictions to that user.

### Requirement 3: Private Labeling Teams

**User Story:** As a portal administrator, I want to create private labeling teams and manage their membership, so that labeling jobs can be assigned to a defined group of labelers.

#### Acceptance Criteria

1. WHEN an authorized administrator creates a Labeling_Team with a name and a Use_Case, THE Portal SHALL persist the Labeling_Team scoped to that Use_Case, and the team SHALL be assignable only to Labeling_Jobs within that same Use_Case.
2. IF a Labeling_Team creation request has an empty name, a name exceeding 128 characters, or a name that duplicates an existing Labeling_Team name within the same Use_Case, THEN THE Portal SHALL reject the request with a validation error identifying the offending name and SHALL create no Labeling_Team.
3. WHEN an authorized administrator adds a portal user with the Data_Labeler role to a Labeling_Team, THE Portal SHALL persist the membership and include the user in Task_Assignment distribution for jobs subsequently assigned to that team.
4. IF an authorized administrator attempts to add a user who does not hold the Data_Labeler role to a Labeling_Team, THEN THE Portal SHALL reject the request with a validation error indicating the missing role and SHALL leave the team's membership unchanged.
5. IF an authorized administrator attempts to add a user who is already a member of the Labeling_Team, THEN THE Portal SHALL reject the request with an error indicating the existing membership and SHALL leave the team's membership unchanged.
6. WHEN an authorized administrator removes a member from a Labeling_Team, THE Portal SHALL persist the removal and exclude the user from Task_Assignment distribution for subsequently created jobs, with the member's unsubmitted Task_Assignments in in-progress jobs handled per Requirement 5.
7. IF a user without administrator authorization attempts to create or modify a Labeling_Team, THEN THE Portal SHALL reject the request with an authorization error and SHALL leave all Labeling_Team data unchanged.
8. WHEN an authorized administrator lists Labeling_Teams for a Use_Case, THE Portal SHALL return only the Labeling_Teams scoped to that Use_Case, including each team's name and current member list with each member's user identity and email address.

### Requirement 4: DDA Labeling Job Creation

**User Story:** As a Job_Creator, I want to create a DDA labeling job with a dataset, modality, label set, instructions, and example images, so that labelers have everything they need to produce consistent labels.

#### Acceptance Criteria

1. WHEN a Job_Creator submits a DDA Labeling_Job, THE DDA_Labeling_System SHALL require a Use_Case, a job name between 1 and 63 characters that is unique among Labeling_Jobs within the Use_Case, an S3 dataset prefix, a Labeling_Modality, and an assigned Labeling_Team, except that WHERE Skip_Verification_Mode is enabled a Labeling_Team is not required.
2. WHERE the Labeling_Modality is Semantic_Segmentation or Object_Detection, THE DDA_Labeling_System SHALL require a Label_Set containing between 1 and 10 distinct, non-empty class names of at most 64 characters each.
3. WHERE the Labeling_Modality is Binary_Classification, THE DDA_Labeling_System SHALL use the fixed Label_Set of normal and anomaly.
4. WHEN a Job_Creator provides labeling instructions text of at most 5,000 characters and up to 10 example images designated as good examples and up to 10 designated as bad examples, each in JPEG or PNG format, THE DDA_Labeling_System SHALL persist the instructions and example image references with the Labeling_Job.
5. WHEN a DDA Labeling_Job is submitted, THE DDA_Labeling_System SHALL validate all other job parameters before enumerating the dataset prefix, and SHALL then enumerate all image objects under the dataset prefix (including objects under nested prefixes) and record the resulting image count with the job.
6. IF the dataset prefix contains zero image objects, THEN THE DDA_Labeling_System SHALL reject job creation with an error identifying the empty prefix.
7. IF an enumerated object under the dataset prefix is not accessible or is not in a supported image format (JPEG or PNG), THEN THE DDA_Labeling_System SHALL reject job creation with an error identifying each offending object.
8. IF the assigned Labeling_Team has zero members with the Data_Labeler role and Skip_Verification_Mode is disabled, THEN THE DDA_Labeling_System SHALL reject job creation with an error identifying the empty team.
9. IF a DDA Labeling_Job submission omits a required parameter or contains a parameter value that fails validation, THEN THE DDA_Labeling_System SHALL reject job creation with an error identifying each missing or invalid parameter.
10. IF DDA Labeling_Job creation is rejected for any reason, THEN THE DDA_Labeling_System SHALL persist no Labeling_Job record and create no Task_Assignments for the rejected submission.
11. WHEN a DDA Labeling_Job submission passes all creation validations, THE DDA_Labeling_System SHALL persist the Labeling_Job with status InProgress and return the job identifier to the Job_Creator.

### Requirement 5: Work Distribution Across Labelers

**User Story:** As a Job_Creator, I want the dataset divided across the labeling team members, so that labelers work concurrently and each person's workload is reduced.

#### Acceptance Criteria

1. WHEN a DDA Labeling_Job with a Labeling_Team completes creation validation and dataset enumeration, THE DDA_Labeling_System SHALL create exactly one Task_Assignment per enumerated dataset image, assigning each image to exactly one member of the assigned Labeling_Team who holds the Data_Labeler role at the time of distribution.
2. WHEN performing the initial distribution of Task_Assignments for a Labeling_Job, THE DDA_Labeling_System SHALL assign each team member a count of images that differs by at most one from every other member's count.
3. WHEN an authorized administrator removes a member from a Labeling_Team that has an InProgress Labeling_Job, THE DDA_Labeling_System SHALL, as part of processing the removal, reassign that member's unsubmitted Task_Assignments across the remaining team members such that the reassigned counts per remaining member differ by at most one, and SHALL retain the removed member's submitted Task_Assignments and their annotations unchanged.
4. IF the last remaining member is removed from a Labeling_Team with an InProgress Labeling_Job, THEN THE DDA_Labeling_System SHALL retain the unsubmitted Task_Assignments in an unassigned state, keep the job status as InProgress, and display a blocked indication for that job to the Job_Creator and authorized administrators until at least one member is added to the team.
5. WHEN a member is added to a Labeling_Team that has one or more blocked Labeling_Jobs, THE DDA_Labeling_System SHALL, for each such job, assign the unassigned Task_Assignments across the team's current members such that the assigned counts per member differ by at most one, and SHALL clear the job's blocked indication.
6. IF Task_Assignment creation for a starting Labeling_Job cannot be completed for every enumerated dataset image, THEN THE DDA_Labeling_System SHALL set the Labeling_Job status to Failed, SHALL not activate a partial set of Task_Assignments for labeling, and SHALL record an error describing the failure on the job.
7. IF a reassignment triggered by member removal or member addition cannot be completed in full, THEN THE DDA_Labeling_System SHALL leave all prior Task_Assignments unchanged and report the failure to the requesting administrator.

### Requirement 6: Email Notifications via SES

**User Story:** As a Data_Labeler, I want to receive an email when a labeling job is ready for me, so that I know when and where to start working.

#### Acceptance Criteria

1. WHEN Task_Assignment distribution completes for a DDA Labeling_Job with a Labeling_Team, THE Notification_Service SHALL send, within 5 minutes, exactly one email through Amazon SES to the portal account email address of each team member holding at least one Task_Assignment in the job, and SHALL send no email to team members holding zero Task_Assignments in the job.
2. THE Notification_Service SHALL include in each notification email the job name, the recipient's assigned image count for the job, and a hyperlink that resolves to the Labeler_Interface sign-in on the Portal and, after authentication, presents that Labeling_Job to the recipient.
3. IF an SES send attempt for a recipient fails, THEN THE Notification_Service SHALL retry the send for that recipient up to 2 additional attempts before treating the recipient's notification as failed.
4. IF all send attempts for a recipient fail, THEN THE Notification_Service SHALL record the failure with the recipient address and the failure reason in the job's record, SHALL continue processing the remaining recipients, and SHALL NOT change the Labeling_Job status as a result of the notification failure.
5. THE Notification_Service SHALL send notification email from the SES sender address configured for the Portal deployment.
6. IF no SES sender address is configured for the Portal deployment, THEN THE DDA_Labeling_System SHALL create the Labeling_Job, record on the Labeling_Job that notifications were skipped, and display the skipped-notification state in the job's detail view to the Job_Creator.
7. WHEN Task_Assignments in a DDA Labeling_Job are assigned after initial distribution to a team member who previously held zero Task_Assignments in that job, THE Notification_Service SHALL send exactly one email through Amazon SES to that member's portal account email address within 5 minutes of the assignment, containing the job name, the member's assigned image count, and the Labeler_Interface sign-in hyperlink for that job.

### Requirement 7: Labeler Interface

**User Story:** As a Data_Labeler, I want a labeling screen that shows one assigned image at a time with the job's instructions and good/bad examples beside it, so that I always know exactly what to label or correct.

#### Acceptance Criteria

1. WHEN a Data_Labeler opens the Labeler_Interface for an in-progress Labeling_Job, THE Labeler_Interface SHALL present exactly one of the labeler's unsubmitted Task_Assignment images at a time, and SHALL never present a Task_Assignment assigned to a different Data_Labeler.
2. WHILE a Task_Assignment image is displayed, THE Labeler_Interface SHALL display the job's instructions text and each stored good and bad example image on the same screen as the image being labeled, and WHERE the Labeling_Job has no stored instructions or example images, THE Labeler_Interface SHALL present the image for labeling without the absent items.
3. WHERE the Labeling_Modality is Binary_Classification, THE Labeler_Interface SHALL provide controls to label the displayed image as exactly one of normal or anomaly.
4. WHERE the Labeling_Modality is Semantic_Segmentation, THE Labeler_Interface SHALL provide mask drawing tools that require each drawn mask region to carry exactly one class selected from the job's Label_Set.
5. WHERE the Labeling_Modality is Object_Detection, THE Labeler_Interface SHALL provide bounding-box drawing tools that require each drawn box to carry exactly one class selected from the job's Label_Set.
6. WHILE a Labeling_Job of a given Labeling_Modality is displayed, THE Labeler_Interface SHALL present only the annotation controls for that Labeling_Modality and SHALL hide the controls of the other two modalities.
7. WHEN a Data_Labeler submits a valid label for the displayed image, THE DDA_Labeling_System SHALL persist the annotation with the submitting user's identity and the submission timestamp, mark the Task_Assignment submitted, and THEN THE Labeler_Interface SHALL present the labeler's next unsubmitted Task_Assignment.
8. IF a Data_Labeler attempts to submit a label that is incomplete for the Labeling_Modality (Binary_Classification with no selection made, Semantic_Segmentation with a drawn region lacking a class, or Object_Detection with a drawn box lacking a class), THEN THE Labeler_Interface SHALL reject the submission, display an error indication identifying the missing element, retain the labeler's in-progress annotation on screen, and leave the Task_Assignment unsubmitted.
9. IF persisting a submitted annotation fails, THEN THE DDA_Labeling_System SHALL leave the Task_Assignment unsubmitted, and THE Labeler_Interface SHALL retain the labeler's annotation on screen and display an error indication that the submission was not saved.
10. WHILE a Data_Labeler has unsubmitted Task_Assignments in a job, THE Labeler_Interface SHALL display the labeler's submitted count and remaining count for that job, and SHALL update both counts after each successful submission.
11. WHEN a Data_Labeler has zero unsubmitted Task_Assignments remaining in a job (excluding Task_Assignments withheld for presentation failure), THE Labeler_Interface SHALL display a completion message for that job that includes the labeler's submitted count and, if any, the count of withheld Task_Assignments.
12. IF a Task_Assignment image cannot be presented to the Data_Labeler because the image object cannot be retrieved or cannot be rendered, THEN THE Labeler_Interface SHALL withhold that Task_Assignment from labeling, THE DDA_Labeling_System SHALL record the presentation failure with the Task_Assignment, and THE Labeler_Interface SHALL continue to the labeler's next presentable Task_Assignment.

### Requirement 8: Model-Assisted Pre-Labeling

**User Story:** As a Job_Creator, I want an option to pre-label images with Segment Anything or other models, including Bedrock models, so that labelers only need to approve or correct each image instead of labeling from scratch.

#### Acceptance Criteria

1. WHERE auto-labeling assist is enabled on a DDA Labeling_Job, THE DDA_Labeling_System SHALL require the Job_Creator to select the Auto_Labeler model from the supported options, which SHALL include at least one Segment Anything (SAM) model and the Amazon Bedrock models available to the Portal.
2. WHERE auto-labeling assist is enabled, THE Auto_Labeler SHALL generate for each dataset image a Pre_Label in the job's Labeling_Modality whose annotation uses only class names from the job's Label_Set, before that image is presented to a Data_Labeler.
3. WHEN a Task_Assignment with a Pre_Label is displayed, THE Labeler_Interface SHALL render the Pre_Label on the image and SHALL provide controls for the Data_Labeler to either approve the Pre_Label as-is or correct it using the modality's annotation controls of the Labeler_Interface before submitting.
4. WHEN a Data_Labeler submits an approved or corrected Pre_Label, THE DDA_Labeling_System SHALL persist the annotation with the submitting Data_Labeler's identity and a timestamp, and SHALL record the annotation as human-annotated for use in the DDA_Manifest metadata.
5. IF the Auto_Labeler fails to generate a valid Pre_Label for an image — including model errors, a model invocation exceeding 120 seconds, unsupported image content, or model output containing class names not in the job's Label_Set — THEN THE DDA_Labeling_System SHALL mark Pre_Label generation for that image as failed, record the failure with the Task_Assignment, and present that image to the assigned Data_Labeler without a Pre_Label.
6. WHILE Pre_Label generation for a Labeling_Job is in progress, THE DDA_Labeling_System SHALL allow Data_Labelers to label images whose Pre_Labels are already available or whose generation has failed, without waiting for the whole job's generation to finish.
7. WHILE Pre_Label generation for a Task_Assignment's image is neither available nor failed, THE Labeler_Interface SHALL withhold that Task_Assignment from presentation to the Data_Labeler until its Pre_Label becomes available or its generation is marked failed.
8. IF the selected Auto_Labeler model does not support the job's Labeling_Modality, THEN THE DDA_Labeling_System SHALL reject job creation with a validation error identifying the incompatible model and Labeling_Modality.

### Requirement 9: Admin Skip-Verification Mode

**User Story:** As a portal administrator, I want to skip human verification and have a selected Bedrock LLM auto-label the whole dataset with per-label prompts, so that I can review the results myself and choose which ones to keep.

#### Acceptance Criteria

1. WHERE Skip_Verification_Mode is requested on a DDA Labeling_Job, THE DDA_Labeling_System SHALL permit the request only for users with administrator authorization and SHALL otherwise reject job creation with an authorization error.
2. WHERE Skip_Verification_Mode is enabled, THE DDA_Labeling_System SHALL require the Job_Creator to select a Bedrock model from the Bedrock models available to the Portal and to provide one non-empty Per_Label_Prompt for each label in the job's Label_Set.
3. IF a Skip_Verification_Mode job submission omits the Bedrock model selection, or omits or provides an empty Per_Label_Prompt for any label in the Label_Set, THEN THE DDA_Labeling_System SHALL reject job creation with a validation error identifying each missing or empty item.
4. WHERE Skip_Verification_Mode is enabled, THE Auto_Labeler SHALL generate an annotation in the job's Labeling_Modality for every dataset image using the selected Bedrock model and the Per_Label_Prompts, and THE DDA_Labeling_System SHALL create zero Task_Assignments and send zero labeler notifications for the job.
5. WHEN every dataset image in a Skip_Verification_Mode job has either an auto-labeled result or a recorded auto-labeling failure, THE DDA_Labeling_System SHALL mark auto-labeling complete and present to the Job_Creator the Admin_Review page listing every dataset image with its auto-labeled result or its failed status.
6. WHEN the Job_Creator accepts or rejects an individual result in the Admin_Review, THE DDA_Labeling_System SHALL persist that per-image decision and SHALL permit the Job_Creator to change any per-image decision until the Admin_Review is finalized.
7. IF the Job_Creator attempts to finalize the Admin_Review while any successfully auto-labeled image has neither an accept nor a reject decision, THEN THE DDA_Labeling_System SHALL reject the finalization with an error identifying the count of undecided images and SHALL leave the Admin_Review open with all persisted decisions retained.
8. IF the Job_Creator attempts to finalize the Admin_Review with zero accepted results, THEN THE DDA_Labeling_System SHALL reject the finalization with an error indicating that at least one accepted result is required and SHALL leave the Admin_Review open.
9. WHEN the Job_Creator finalizes the Admin_Review, THE Manifest_Generator SHALL include exactly the accepted results in the output DDA_Manifest and exclude all rejected results and all failed images.
10. IF the Bedrock model invocation fails for an image in Skip_Verification_Mode, THEN THE DDA_Labeling_System SHALL record the failure and its reason for that image and present the image in the Admin_Review as failed and ineligible for acceptance.
11. WHEN the Admin_Review is finalized and the accepted results are written to the DDA_Manifest, THE Manifest_Generator SHALL set the human-annotated field of each written entry's label metadata to indicate machine annotation.

### Requirement 10: DDA Manifest Output

**User Story:** As a Job_Creator, I want a completed DDA labeling job to produce the exact manifest format that existing compile and training jobs consume, so that no conversion or special treatment is needed downstream.

#### Acceptance Criteria

1. WHEN all Task_Assignments of a DDA Labeling_Job are submitted, or WHEN a Skip_Verification_Mode Admin_Review is finalized, THE Manifest_Generator SHALL write a DDA_Manifest to the Use_Case's configured output bucket and record the resulting manifest S3 URI on the Labeling_Job.
2. THE Manifest_Generator SHALL serialize the DDA_Manifest as JSON Lines with exactly one JSON object per included image, where included images are those with a submitted annotation for jobs with a Labeling_Team, and those with an accepted Admin_Review result for Skip_Verification_Mode jobs, and all other images SHALL be excluded from the manifest.
3. WHERE the Labeling_Modality is Binary_Classification, THE Manifest_Generator SHALL emit for each included image the fields `source-ref` containing the image's S3 URI, `anomaly-label` (0 for normal, 1 for anomaly), and `anomaly-label-metadata` containing the class-name matching the assigned label, a confidence value between 0 and 1 inclusive, a type identifying the classification task, the job-name of the Labeling_Job, a human-annotated indicator reflecting whether the annotation was human-submitted or machine-annotated per the Skip_Verification_Mode rule, and a creation-date equal to the annotation's persisted timestamp.
4. WHERE the Labeling_Modality is Semantic_Segmentation, THE Manifest_Generator SHALL emit for each included image the Binary_Classification fields of criterion 3, and SHALL additionally render the image's mask annotations as a PNG mask object written to the Use_Case's configured output bucket with pixel dimensions equal to the source image and one distinct color per Label_Set class plus a background color, and SHALL emit `anomaly-mask-ref` containing the mask object's S3 URI and `anomaly-mask-ref-metadata` containing the internal color map that associates each Label_Set class name with its assigned color, using the same class-to-color mapping for every image in the job.
5. WHERE the Labeling_Modality is Object_Detection, THE Manifest_Generator SHALL emit for each included image a bounding-box label attribute containing the image size in pixels and the box annotations with pixel coordinates that lie within the image boundaries and class ids assigned as zero-based indexes into the Label_Set order, and its `-metadata` companion containing the class map that associates each class id with its Label_Set class name, in the SageMaker Ground Truth bounding-box output structure.
6. THE Manifest_Generator SHALL emit manifests that pass the Portal's existing manifest validation for the job's task type without transformation.
7. FOR ALL sets of persisted annotations, serializing them to a DDA_Manifest and parsing that manifest back SHALL produce annotations equivalent to the originals, where equivalence means the same set of source image references and, per image, the same class assignment for Binary_Classification, pixel-identical class regions for Semantic_Segmentation, and identical box coordinates and class ids for Object_Detection (round-trip property).
8. WHEN a DDA Labeling_Job completes, THE Portal SHALL expose the job's output manifest S3 URI in the same job fields used by SageMaker Ground Truth jobs, so that training and compilation job creation consume DDA jobs and Ground Truth jobs identically.
9. IF writing the DDA_Manifest or a mask object to S3 fails, THEN THE DDA_Labeling_System SHALL record no manifest S3 URI on the Labeling_Job, retain all persisted annotations unchanged, set the job status to Failed, and surface an error indicating the manifest generation failure to the Job_Creator.

### Requirement 11: Job Monitoring and Lifecycle

**User Story:** As a Job_Creator, I want to monitor progress and manage the lifecycle of DDA labeling jobs, so that I can track labeling work and intervene when needed.

#### Acceptance Criteria

1. WHEN an authorized user views a DDA Labeling_Job, THE Portal SHALL display the job status, the total image count recorded at job creation, the count of submitted Task_Assignments, and the progress percentage computed as the submitted count divided by the total image count, expressed as a percentage rounded to the nearest whole number.
2. WHEN an authorized user views a DDA Labeling_Job with a Labeling_Team, THE Portal SHALL display, for each current team member, that member's submitted and remaining Task_Assignment counts, and SHALL display the count of unassigned Task_Assignments when one or more exist.
3. THE DDA_Labeling_System SHALL represent job status using exactly the values InProgress, Completed, Failed, and Stopped, consistent with the existing labeling job status values, and SHALL set the status of a successfully created DDA Labeling_Job to InProgress.
4. WHEN a Job_Creator stops a DDA Labeling_Job in InProgress status, THE DDA_Labeling_System SHALL set the job status to Stopped, record the stop timestamp, and retain all annotations already submitted for that job, regardless of how many Task_Assignments have been submitted.
5. IF a stop operation cannot be completed in full, THEN THE DDA_Labeling_System SHALL leave the Labeling_Job in InProgress status and SHALL report the failure to the Job_Creator with an error indication that the job was not stopped.
6. WHEN the last unsubmitted Task_Assignment of a DDA Labeling_Job is submitted, or WHEN a Skip_Verification_Mode Admin_Review is finalized, THE DDA_Labeling_System SHALL set the job status to Completed and record the completion timestamp.
7. WHEN a Labeling_Job is created, stopped, or completed, THE Portal SHALL record an audit event containing the acting user identity, the job identifier, the event type, and the event timestamp.
8. IF a label submission is received for a Labeling_Job in Stopped status, THEN THE DDA_Labeling_System SHALL reject the submission without persisting the annotation and SHALL return an error indicating the job has been stopped.
9. IF a stop request targets a DDA Labeling_Job whose status is not InProgress, THEN THE DDA_Labeling_System SHALL reject the request with a validation error and SHALL leave the job status unchanged.
10. WHERE Skip_Verification_Mode is enabled on a DDA Labeling_Job, THE Portal SHALL display, in place of the submitted Task_Assignment count, the count of images whose auto-label attempts have completed (succeeded or failed) as the submitted count used for the progress percentage.

### Requirement 12: Data Access and Storage

**User Story:** As a portal administrator, I want labeling data handled through the portal's existing account and storage model, so that labeling respects the same data boundaries as training and compilation.

#### Acceptance Criteria

1. WHEN reading dataset images for labeling or auto-labeling, THE DDA_Labeling_System SHALL access the Use_Case's configured data bucket using the Portal's existing cross-account role mechanism.
2. IF the cross-account role mechanism fails for a Use_Case whose data bucket is directly accessible to the Portal account, THEN THE DDA_Labeling_System SHALL access the bucket directly using the Portal's own permissions.
3. IF neither the cross-account role mechanism nor direct Portal-account access grants access to a required bucket, THEN THE DDA_Labeling_System SHALL fail the requested operation without performing partial writes, record the access failure with the Labeling_Job, and report an error to the requesting user indicating the inaccessible bucket.
4. WHEN writing masks, manifests, and job artifacts, THE DDA_Labeling_System SHALL write to the Use_Case's configured output bucket used by the existing labeling flow, using the same cross-account role mechanism and direct-access fallback as dataset reads.
5. IF a write of a mask, manifest, or job artifact to the output bucket fails, THEN THE DDA_Labeling_System SHALL retain all previously persisted annotations, SHALL NOT set the Labeling_Job status to Completed, and SHALL report an error to the Job_Creator indicating the failed write.
6. WHEN the Labeler_Interface displays a dataset image to a Data_Labeler, THE Portal SHALL serve the image through a time-limited access mechanism that grants read-only access, is scoped to that single image object, and expires no more than 15 minutes after issuance.
7. IF a request presents an expired time-limited access grant for an image, THEN THE Portal SHALL deny the request, and THE Labeler_Interface SHALL obtain a new time-limited access grant for that image without discarding the Data_Labeler's in-progress annotation.
8. THE DDA_Labeling_System SHALL persist Labeling_Jobs, Labeling_Teams, Task_Assignments, and annotations in portal-managed DynamoDB tables and S3 objects, with each persisted record associated with exactly one Use_Case.
