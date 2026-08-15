/**
 * Pure, UI-free compilation-status helpers shared by CompilationTab and its
 * property tests (see .kiro/specs/onnx-compile-error-diagnostics/).
 *
 * Kept free of React/Cloudscape imports so fast-check can exercise the
 * classification and the diagnostic predicate directly.
 */

/** Diagnostic-relevant subset of a compilation job entry. */
export interface CompilationJobDiagnostics {
  status?: string;
  failure_reason?: string;
  error?: string;
  poll_error?: string;
}

/**
 * Uppercase-normalize a status so classification is identical for a value,
 * its uppercase form, and its lowercase form ('Failed' / 'FAILED' / 'failed').
 * Backend statuses arrive in mixed case: portal-synthesized entries use
 * 'InProgress' / 'Failed', while SageMaker describe responses are stored
 * verbatim in uppercase ('INPROGRESS', 'COMPLETED', 'FAILED').
 */
export const normalizeCompilationStatus = (status?: string): string =>
  String(status || '').toUpperCase();

/**
 * Diagnostic predicate for the "Compilation Errors" panel: a job's
 * diagnostics must be surfaced when its normalized status is FAILED /
 * STOPPED / ERROR, or when it carries a recorded reason (failure_reason /
 * error) or a poll fault (poll_error). This surfaces the ONNX no-live-job
 * reason and the Neo FAILED reasons that the previous exact-match
 * `status === 'Failed'` filter always excluded.
 */
export const isDiagnosticCompilationJob = (
  job: CompilationJobDiagnostics
): boolean => {
  const s = normalizeCompilationStatus(job.status);
  return (
    s === 'FAILED' ||
    s === 'STOPPED' ||
    s === 'ERROR' ||
    Boolean(job.failure_reason) ||
    Boolean(job.error) ||
    Boolean(job.poll_error)
  );
};
