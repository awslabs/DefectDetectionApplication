/**
 * Plain-language explanations of Sync_Operation failures (custom-node-
 * source-lifecycle 4.11, design "Error Handling").
 */
import type { SyncFailureCategory, SyncOperation } from './types';

export interface SyncFailureView {
  header: string;
  message: string;
  /** Hint on what the user can do next. */
  action?: string;
}

const HEADERS: Record<SyncFailureCategory, string> = {
  authentication: 'Access token rejected',
  not_found: 'Repository, branch, ref, or path not found',
  unreachable: 'Git host unreachable',
  diverged: 'Repository changed since the last sync',
  push_rejected: 'Push rejected',
  invalid_source: 'Pulled source rejected',
  internal: 'Unexpected sync failure',
};

export function describeSyncFailure(
  category: SyncFailureCategory | undefined,
  failure?: SyncOperation['failure'] | null,
  path?: string
): SyncFailureView {
  const detail = failure?.message ? ` (${failure.message})` : '';
  switch (category) {
    case 'authentication':
      return {
        header: HEADERS.authentication,
        message: `The Git host rejected the access token${detail}.`,
        action: 'Update the token on the Git connection and verify it again.',
      };
    case 'not_found':
      return {
        header: HEADERS.not_found,
        message: `The repository, branch, ref, or path does not exist${detail}.`,
        action: 'Check the connection URL, the linked branch, and the repository path.',
      };
    case 'unreachable':
      return {
        header: HEADERS.unreachable,
        message: `The Git host could not be reached${detail}.`,
        action: 'Check the repository URL and network access, then retry.',
      };
    case 'diverged': {
      const files = failure?.changed_files?.length
        ? ` Changed files: ${failure.changed_files.join(', ')}.`
        : '';
      return {
        header: HEADERS.diverged,
        message: `The repository changed under ${path ?? 'the linked path'} since the last sync.${files}`,
        action: 'Pull first to bring those changes in, or push with overwrite to replace them.',
      };
    }
    case 'push_rejected':
      return {
        header: HEADERS.push_rejected,
        message: `The branch moved while the push ran and the retry was rejected${detail}.`,
        action: 'Retry the push.',
      };
    case 'invalid_source': {
      const defects = failure?.defects?.length ? ` Defects: ${failure.defects.join('; ')}.` : '';
      const limit = failure?.limit ? ` Exceeded limit: ${failure.limit}.` : '';
      return {
        header: HEADERS.invalid_source,
        message: `The pulled tree cannot be installed${detail}.${defects}${limit}`,
        action: 'Fix the repository contents and pull again.',
      };
    }
    case 'internal':
    default:
      return {
        header: HEADERS.internal,
        message: `The sync runner failed${detail}.`,
        action: 'See the log excerpt; retry once the cause is resolved.',
      };
  }
}
