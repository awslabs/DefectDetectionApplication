/**
 * AccountErrorNotice — renders the use-case account refusals of
 * `POST /usecases` and `POST /usecases/verify-role` by `error_code` (spec:
 * portal-authorization-and-iam-scoping, design I1 "Error handling"; task
 * 8.1):
 *
 * - `ACCOUNT_NOT_TRUSTED`: the account is not in the portal's trusted
 *   use-case account list (`trustedUseCaseAccountIds`);
 * - `ACCOUNT_CONTROL_NOT_VERIFIED`: the portal could not assume the given
 *   role with the given external id (proof of control).
 *
 * The server's `error` is shown as the body and its `remedy`, when present,
 * as the next step. Every other error is left to the caller's generic
 * message (`getErrorMessage`).
 */
import { Alert, Box, SpaceBetween } from '@cloudscape-design/components';
import { ApiError } from '../services/api';

export type AccountErrorCode = 'ACCOUNT_NOT_TRUSTED' | 'ACCOUNT_CONTROL_NOT_VERIFIED';

export const ACCOUNT_ERROR_CODES: readonly AccountErrorCode[] = [
  'ACCOUNT_NOT_TRUSTED',
  'ACCOUNT_CONTROL_NOT_VERIFIED',
];

export const ACCOUNT_ERROR_HEADINGS: Record<AccountErrorCode, string> = {
  ACCOUNT_NOT_TRUSTED: 'This account is not trusted by the portal',
  ACCOUNT_CONTROL_NOT_VERIFIED: 'The portal could not verify control of this account',
};

/** The account error code of a caught error, or undefined for any other error. */
export function accountErrorCode(err: unknown): AccountErrorCode | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const code = err.details?.error_code;
  return typeof code === 'string' && (ACCOUNT_ERROR_CODES as readonly string[]).includes(code)
    ? (code as AccountErrorCode)
    : undefined;
}

interface AccountErrorNoticeProps {
  /** The caught error; nothing is rendered unless it carries an account error code. */
  err: unknown;
  onDismiss?: () => void;
}

export default function AccountErrorNotice({ err, onDismiss }: AccountErrorNoticeProps): JSX.Element | null {
  const code = accountErrorCode(err);
  if (!code || !(err instanceof ApiError)) return null;
  const detail = err.details?.error;
  const message = (typeof detail === 'string' && detail) || err.message;
  const remedy = typeof err.details?.remedy === 'string' ? err.details.remedy : '';
  return (
    <Alert
      type="error"
      header={ACCOUNT_ERROR_HEADINGS[code]}
      dismissible={!!onDismiss}
      onDismiss={onDismiss}
    >
      <SpaceBetween size="xs">
        <Box>{message}</Box>
        {remedy && (
          <Box>
            <strong>Next step:</strong> {remedy}
          </Box>
        )}
      </SpaceBetween>
    </Alert>
  );
}
