/**
 * Shared error-handling helpers for portal forms.
 *
 * Goals (matching the Create Labeling Job UX):
 * - Always surface the REAL backend/SageMaker message instead of a generic one.
 * - Make the error visible by scrolling it into view.
 *
 * The API client (services/api.ts) already throws an Error whose `message` is
 * the backend's `error` field when present, so `getErrorMessage` mostly needs
 * to unwrap the various shapes errors can arrive in.
 */

/**
 * Extract a human-readable message from any thrown value.
 *
 * @param err      The caught error (unknown shape).
 * @param fallback Message to use when nothing better can be extracted.
 */
export function getErrorMessage(err: unknown, fallback = 'Something went wrong. Please try again.'): string {
  if (!err) return fallback;

  // Plain string thrown
  if (typeof err === 'string') return err || fallback;

  if (typeof err === 'object') {
    const anyErr = err as any;

    // Standard Error instance
    if (err instanceof Error && typeof err.message === 'string' && err.message.trim()) {
      return err.message;
    }

    // Backend response shapes: { error }, { message }, { errors: [...] }
    if (typeof anyErr.error === 'string' && anyErr.error.trim()) return anyErr.error;
    if (typeof anyErr.message === 'string' && anyErr.message.trim()) return anyErr.message;
    if (Array.isArray(anyErr.errors) && anyErr.errors.length) {
      const msgs = anyErr.errors
        .map((e: any) => (typeof e === 'string' ? e : e?.message))
        .filter(Boolean);
      if (msgs.length) return msgs.join(', ');
    }
  }

  return fallback;
}

/**
 * Scroll the window to the top so a top-of-page error alert is visible.
 * Safe to call in non-browser/test environments.
 */
export function scrollToTop(): void {
  if (typeof window !== 'undefined' && typeof window.scrollTo === 'function') {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

/**
 * Convenience: turn a caught error into a message and scroll it into view.
 * Returns the message so callers can `setError(handleFormError(err, ...))`.
 */
export function handleFormError(err: unknown, fallback?: string): string {
  const message = getErrorMessage(err, fallback);
  scrollToTop();
  return message;
}
