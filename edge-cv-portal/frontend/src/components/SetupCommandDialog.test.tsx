/**
 * Component tests for `SetupCommandDialog`
 * (station-quick-setup task 9.5, Requirement 2.4).
 *
 * Covers the two things Requirement 2.4 requires the portal to do once the
 * Setup_Command is generated:
 *   - display a copy-to-clipboard control that places the *complete*
 *     Setup_Command on the clipboard in a single user action, and
 *   - display the Setup_Token expiration date and time.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import SetupCommandDialog, { formatExpiration } from './SetupCommandDialog';

const SETUP_COMMAND =
  'curl -fsSL https://portal.example.com/v1/quick-setup/bootstrap -o /tmp/dda-qs.sh && ' +
  'echo "abc123  /tmp/dda-qs.sh" | sha256sum -c - && ' +
  'sudo bash /tmp/dda-qs.sh --endpoint https://portal.example.com/v1/quick-setup --token dqs1.reg-1.secret';

const TOKEN_EXPIRES_AT = 1730005400; // epoch seconds

let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  // Cloudscape's CopyToClipboard uses navigator.clipboard.writeText; jsdom
  // does not implement it, so install a spyable stub.
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('formatExpiration', () => {
  it('renders epoch seconds as a local date-time string', () => {
    expect(formatExpiration(TOKEN_EXPIRES_AT)).toBe(
      new Date(TOKEN_EXPIRES_AT * 1000).toLocaleString()
    );
  });
});

describe('SetupCommandDialog (Requirement 2.4)', () => {
  it('displays the complete Setup_Command in a monospace code block', () => {
    render(
      <SetupCommandDialog
        setupCommand={SETUP_COMMAND}
        tokenExpiresAt={TOKEN_EXPIRES_AT}
        onDismiss={vi.fn()}
      />
    );
    // Match the exact command (including its internal double space, which
    // Testing Library's text normalization would otherwise collapse).
    const code = document.querySelector('code');
    expect(code).not.toBeNull();
    expect(code?.textContent).toBe(SETUP_COMMAND);
  });

  it('displays the token expiration date and time', () => {
    render(
      <SetupCommandDialog
        setupCommand={SETUP_COMMAND}
        tokenExpiresAt={TOKEN_EXPIRES_AT}
        onDismiss={vi.fn()}
      />
    );
    expect(screen.getByText('Token expires')).toBeInTheDocument();
    expect(
      screen.getByText(new Date(TOKEN_EXPIRES_AT * 1000).toLocaleString())
    ).toBeInTheDocument();
  });

  it('copies the entire command to the clipboard in a single action', async () => {
    render(
      <SetupCommandDialog
        setupCommand={SETUP_COMMAND}
        tokenExpiresAt={TOKEN_EXPIRES_AT}
        onDismiss={vi.fn()}
      />
    );

    // One click of the single copy control writes the complete command.
    fireEvent.click(screen.getByRole('button', { name: /copy/i }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText).toHaveBeenCalledWith(SETUP_COMMAND);
  });

  it('invokes onDismiss from the Done control', () => {
    const onDismiss = vi.fn();
    render(
      <SetupCommandDialog
        setupCommand={SETUP_COMMAND}
        tokenExpiresAt={TOKEN_EXPIRES_AT}
        deviceName="station-42"
        onDismiss={onDismiss}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
