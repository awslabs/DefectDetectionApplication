/**
 * **Feature: build-source-selection, Property 12: Frontend source selection behavior**
 *
 * _For any_ configured default repository the submission form SHALL
 * pre-fill the repository field with it; _for any_ repository value the
 * branch dropdown SHALL be populated from Discovery for that repository
 * and SHALL re-populate when the repository changes; _for any_ Discovery
 * outcome exactly one of loading / actionable error / options SHALL be
 * presented, with manual ref entry and submission still available on
 * failure.
 *
 * **Validates: Requirements 1.1, 1.2, 2.1, 2.2, 2.3, 2.6**
 *
 * `fast-check` over configured defaults, repository values, and the
 * discovery-outcome domain (pattern:
 * `src/components/vllm-publish/publishState.gating.property.test.ts`),
 * driving the real `BuildsPage` component against a mocked `apiService`.
 * Discovery is debounced 500 ms in the component, so each property run
 * renders, waits the debounce out with real timers, and asserts on the
 * settled DOM; run counts are kept low because every run is a full
 * component render.
 *
 * The unit test for Req 1.2 (the submit body omits `repository` and
 * `source_ref` when the fields are untouched) lives at the bottom;
 * the `BuildDetail` source-row unit tests (Req 2.6) are in
 * `BuildDetail.test.tsx`.
 */

import { describe, expect, it, vi } from 'vitest';
import * as fc from 'fast-check';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import BuildsPage, { DISCOVERY_ERROR_MESSAGES } from './BuildsPage';
import { ApiError } from '../../services/api';
import type { BuildBranchesResponse, BuildJob } from './types';

const {
  listBuilds,
  listBuildServers,
  submitBuild,
  cancelBuild,
  retryBuild,
  getBuildConfig,
  listBuildBranches,
  navigateMock,
} = vi.hoisted(() => ({
  listBuilds: vi.fn(),
  listBuildServers: vi.fn(),
  submitBuild: vi.fn(),
  cancelBuild: vi.fn(),
  retryBuild: vi.fn(),
  getBuildConfig: vi.fn(),
  listBuildBranches: vi.fn(),
  navigateMock: vi.fn(),
}));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      listBuilds,
      listBuildServers,
      submitBuild,
      cancelBuild,
      retryBuild,
      getBuildConfig,
      listBuildBranches,
    },
  };
});

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

const DEFAULT_REPO = 'https://github.com/awslabs/DefectDetectionApplication';

const CREATED_JOB: BuildJob = {
  build_job_id: 'job-new-1',
  request_id: 'r-1',
  request_order: 0,
  predecessor_job_id: null,
  build_target: 'JP5',
  component_name: 'aws.edgeml.dda.LocalServer.arm64JP5',
  required_arch: 'arm64',
  execution_mode: 'ephemeral',
  server_id: null,
  status: 'queued',
  requested_by: 'alice',
  created_at: 1_700_000_000_000,
};

/** Reset every apiService mock to the zero-effort happy path. Called at
 *  the start of each property run (beforeEach only runs per `it`). */
function setupMocks() {
  vi.clearAllMocks();
  listBuilds.mockResolvedValue({ jobs: [], nextToken: null, total: 0 });
  listBuildServers.mockResolvedValue({ servers: [] });
  submitBuild.mockResolvedValue({ request_id: 'r-1', jobs: [CREATED_JOB] });
  getBuildConfig.mockResolvedValue({
    config: { default_repository: DEFAULT_REPO },
  });
  listBuildBranches.mockResolvedValue({
    branches: ['main'],
    default_branch: 'main',
    truncated: false,
  });
}

function repositoryInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector(
    'input[aria-label="Repository"]'
  ) as HTMLInputElement;
}

function refNativeInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector(
    'input[aria-label="Branch or ref"]'
  ) as HTMLInputElement;
}

async function renderPage() {
  const utils = render(<BuildsPage />);
  // Initial load settled once the empty-history state renders.
  await screen.findByText('No builds');
  return utils;
}

// ------------------------------------------------------------- generators

/** `<owner>/<repo>` GitHub HTTPS URLs (the shape the validator accepts). */
const repoUrlArb: fc.Arbitrary<string> = fc
  .tuple(
    fc.stringMatching(/^[a-z][a-z0-9-]{0,10}$/),
    fc.stringMatching(/^[A-Za-z][A-Za-z0-9-]{0,14}$/)
  )
  .map(([owner, repo]) => `https://github.com/${owner}/${repo}`);

/** Branch names in the discovered-branch shape (no whitespace). */
const branchNameArb: fc.Arbitrary<string> = fc.stringMatching(
  /^[a-z][a-z0-9-]{0,14}$/
);

/** A successful discovery response: >= 1 branch, one flagged default. */
const discoverySuccessArb: fc.Arbitrary<BuildBranchesResponse> = fc
  .tuple(
    fc.uniqueArray(branchNameArb, { minLength: 1, maxLength: 5 }),
    fc.nat()
  )
  .map(([branches, pick]) => ({
    branches,
    default_branch: branches[pick % branches.length],
    truncated: false,
  }));

/** The six distinct discovery error codes GET /build-branches returns. */
const discoveryErrorCodeArb: fc.Arbitrary<string> = fc.constantFrom(
  ...Object.keys(DISCOVERY_ERROR_MESSAGES)
);

/** The discovery-outcome domain: settled success, settled coded failure,
 *  or still in flight (a never-resolving call). */
type DiscoveryOutcome =
  | { kind: 'success'; response: BuildBranchesResponse }
  | { kind: 'error'; code: string }
  | { kind: 'loading' };

const discoveryOutcomeArb: fc.Arbitrary<DiscoveryOutcome> = fc.oneof(
  discoverySuccessArb.map<DiscoveryOutcome>((response) => ({
    kind: 'success',
    response,
  })),
  discoveryErrorCodeArb.map<DiscoveryOutcome>((code) => ({
    kind: 'error',
    code,
  })),
  fc.constant<DiscoveryOutcome>({ kind: 'loading' })
);

/** Manually enterable refs: branches, tags, and 40-hex SHAs (Req 2.7's
 *  accepted shapes; no whitespace so trim() is the identity). */
const manualRefArb: fc.Arbitrary<string> = fc.oneof(
  fc.stringMatching(/^[a-z][a-z0-9._-]{0,15}$/),
  fc.stringMatching(/^v[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}$/),
  fc.stringMatching(/^[0-9a-f]{40}$/),
  branchNameArb.map((name) => `feature/${name}`)
);

// ------------------------------------------------------------------ tests

describe('Property 12: Frontend source selection behavior', () => {
  it(
    'the repository field pre-fills with the configured default repository (Req 1.1)',
    async () => {
      await fc.assert(
        fc.asyncProperty(repoUrlArb, async (configuredDefault) => {
          setupMocks();
          getBuildConfig.mockResolvedValue({
            config: { default_repository: configuredDefault },
          });
          try {
            const { container } = await renderPage();
            await waitFor(() =>
              expect(repositoryInput(container).value).toBe(configuredDefault)
            );
          } finally {
            cleanup();
          }
        }),
        { numRuns: 10 }
      );
    },
    180_000
  );

  it(
    'the branch options come from discovery for the current repository and re-populate when it changes (Req 2.1, 2.2)',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          fc
            .tuple(repoUrlArb, repoUrlArb)
            .filter(([a, b]) => a !== b),
          discoverySuccessArb,
          discoverySuccessArb,
          async ([repoA, repoB], discoveryA, discoveryB) => {
            setupMocks();
            getBuildConfig.mockResolvedValue({
              config: { default_repository: repoA },
            });
            listBuildBranches.mockImplementation(async (repo: string) =>
              repo === repoA ? discoveryA : discoveryB
            );
            try {
              const { container } = await renderPage();
              const autosuggest = createWrapper(container).findAutosuggest()!;

              // Discovery runs (debounced) for the pre-filled repository,
              // and the dropdown lists exactly its branches with the
              // default branch annotated (Req 2.1).
              await waitFor(
                () =>
                  expect(listBuildBranches).toHaveBeenCalledWith(repoA),
                { timeout: 4000 }
              );
              autosuggest.focus();
              const assertOptionsMatch = (
                discovery: BuildBranchesResponse
              ) => {
                const texts = autosuggest
                  .findDropdown()
                  .findOptions()
                  .map((option) => option.getElement().textContent || '');
                expect(texts).toHaveLength(discovery.branches.length);
                discovery.branches.forEach((branch, i) => {
                  expect(texts[i]).toContain(branch);
                });
                // Exactly the default branch carries the annotation.
                texts.forEach((text, i) => {
                  expect(text.includes('default branch')).toBe(
                    discovery.branches[i] === discovery.default_branch
                  );
                });
              };
              await waitFor(() => assertOptionsMatch(discoveryA), {
                timeout: 4000,
              });

              // Changing the repository re-runs discovery against the new
              // value and re-populates the options (Req 2.2).
              fireEvent.change(repositoryInput(container), {
                target: { value: repoB },
              });
              await waitFor(
                () =>
                  expect(listBuildBranches).toHaveBeenCalledWith(repoB),
                { timeout: 4000 }
              );
              autosuggest.focus();
              await waitFor(() => assertOptionsMatch(discoveryB), {
                timeout: 4000,
              });
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 6 }
      );
    },
    180_000
  );

  it(
    'exactly one of loading / actionable error / options is presented for any discovery outcome (Req 2.3)',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          repoUrlArb,
          discoveryOutcomeArb,
          async (repo, outcome) => {
            setupMocks();
            getBuildConfig.mockResolvedValue({
              config: { default_repository: repo },
            });
            listBuildBranches.mockImplementation(() => {
              if (outcome.kind === 'success') {
                return Promise.resolve(outcome.response);
              }
              if (outcome.kind === 'error') {
                return Promise.reject(
                  new ApiError('discovery failed', 502, outcome.code)
                );
              }
              // Still in flight: the loading state must persist.
              return new Promise<never>(() => {});
            });
            try {
              const { container } = await renderPage();
              await waitFor(
                () => expect(listBuildBranches).toHaveBeenCalledWith(repo),
                { timeout: 4000 }
              );
              const autosuggest = createWrapper(container).findAutosuggest()!;
              autosuggest.focus();

              await waitFor(
                () => {
                  const loadingShown =
                    screen.queryAllByText('Discovering branches').length > 0;
                  const errorShown = Object.values(
                    DISCOVERY_ERROR_MESSAGES
                  ).some(
                    (message) => screen.queryAllByText(message).length > 0
                  );
                  const optionsShown =
                    autosuggest.findDropdown().findOptions().length > 0;

                  // Exactly one presentation at the settled state.
                  expect(
                    Number(loadingShown) +
                      Number(errorShown) +
                      Number(optionsShown)
                  ).toBe(1);

                  // And it is the one the outcome dictates, the error
                  // being the actionable per-code message (Req 2.3, 3.3).
                  if (outcome.kind === 'success') {
                    expect(optionsShown).toBe(true);
                  } else if (outcome.kind === 'error') {
                    expect(
                      screen.queryAllByText(
                        DISCOVERY_ERROR_MESSAGES[outcome.code]
                      ).length
                    ).toBeGreaterThan(0);
                  } else {
                    expect(loadingShown).toBe(true);
                  }
                },
                { timeout: 4000 }
              );
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 8 }
      );
    },
    180_000
  );

  it(
    'submission stays possible with a manually entered ref when discovery fails (Req 2.3)',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          manualRefArb,
          discoveryErrorCodeArb,
          async (manualRef, errorCode) => {
            setupMocks();
            listBuildBranches.mockRejectedValue(
              new ApiError('discovery failed', 502, errorCode)
            );
            try {
              const { container } = await renderPage();
              const wrapper = createWrapper(container);

              // The discovery failure has settled into the actionable
              // error state before the user proceeds manually.
              const autosuggest = wrapper.findAutosuggest()!;
              autosuggest.focus();
              await waitFor(
                () =>
                  expect(
                    screen.queryAllByText(DISCOVERY_ERROR_MESSAGES[errorCode])
                      .length
                  ).toBeGreaterThan(0),
                { timeout: 4000 }
              );

              // Manual entry and submission still work (Req 2.3): type a
              // ref, pick a target, submit.
              fireEvent.change(refNativeInput(container), {
                target: { value: manualRef },
              });
              const targets = wrapper.findMultiselect()!;
              targets.openDropdown();
              targets.selectOptionByValue('JP5');
              fireEvent.click(
                screen.getByRole('button', { name: 'Submit build request' })
              );

              await waitFor(() =>
                expect(submitBuild).toHaveBeenCalledTimes(1)
              );
              // The untouched repository (still the configured default)
              // is omitted; the manual ref rides as source_ref.
              expect(submitBuild).toHaveBeenCalledWith({
                targets: ['JP5'],
                execution_mode: 'ephemeral',
                source_ref: manualRef,
              });
            } finally {
              cleanup();
            }
          }
        ),
        { numRuns: 6 }
      );
    },
    180_000
  );
});

describe('Submit body source fields (build-source-selection Requirement 1.2)', () => {
  it('omits repository and source_ref when both fields are untouched', async () => {
    setupMocks();
    try {
      const { container } = await renderPage();
      // The repository pre-fill has landed (so the field is non-empty
      // and equal to the default, the untouched state).
      await waitFor(() =>
        expect(repositoryInput(container).value).toBe(DEFAULT_REPO)
      );

      const targets = createWrapper(container).findMultiselect()!;
      targets.openDropdown();
      targets.selectOptionByValue('JP6');
      fireEvent.click(
        screen.getByRole('button', { name: 'Submit build request' })
      );

      await waitFor(() => expect(submitBuild).toHaveBeenCalledTimes(1));
      // Byte-identical zero-effort body: no source-selection keys at all
      // (Req 1.2, 7.1).
      const body = submitBuild.mock.calls[0][0];
      expect(body).toEqual({ targets: ['JP6'], execution_mode: 'ephemeral' });
      expect('repository' in body).toBe(false);
      expect('source_ref' in body).toBe(false);
    } finally {
      cleanup();
    }
  });
});
