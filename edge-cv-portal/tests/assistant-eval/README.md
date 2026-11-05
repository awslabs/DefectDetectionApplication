# Portal assistant: evaluation set and smoke check

Spec `.kiro/specs/portal-ai-assistant/`, Requirements 14.5 and 14.6, design D9.
Both run against a deployed Portal and record identifiers, tool names,
categories and durations only.

## Prerequisites

- A test account in the Portal user pool with access to a test Use_Case
  (the smoke check's toggle step needs a PortalAdmin; the audit-filter step
  needs `view_audit_logs`).
- An ID token for that account in `DDA_ID_TOKEN`, or `--username`,
  `--password` and `--client-id` of a throwaway app client that allows
  `USER_PASSWORD_AUTH` (the web client allows SRP only; see the
  Administrator Guide on test accounts).
- The assistant API URL (`EdgeCVPortalAssistantStack` output
  `AssistantApiUrl`) and the portal API URL (`EdgeCVPortalComputeStack`
  output `ApiUrl`), for the audit check and for the action checks.
- Actions on (Settings → Assistant → Actions, the default) and
  `labeling-teams:manage` in the test use case: the action steps create
  labeling teams there.

## Actions and the test objects they create

Each confirmation card a turn shows gets the next decision listed for its
case or step (`confirm`, `reject` or `expire`); any further card is
rejected, so a run changes nothing it did not ask for. The action steps
create labeling teams in the test use case, named `asst-eval-<run>-…` or
`asst-smoke-<run>-…`. Through the portal API the runner checks that each
team exists exactly when its card was confirmed, then deletes every team of
the run, and reports how many it deleted (a non-zero "not deleted" count
fails the run). Every case also fails when a state-changing tool ran
without a confirmation, or when a declined proposal was shown again in the
same turn.

## Smoke check (14.6)

```bash
python3 run_eval.py smoke --api "$ASSISTANT_API_URL" --portal-api "$API_URL" --usecase-id "$USECASE_ID"
```

Checks: state enabled and actions on by default; a guidance turn completes
and cites the guide; a read-only tool turn completes and ran a read-only
tool; a confirmed action (creating a labeling team) shows its card naming
the use case, runs once and creates the team; a rejected action invokes
nothing; the Audit Logs `channel=assistant` filter shows the turns' entries,
including the confirmed action's; the test teams are deleted; Settings
toggle off hides the assistant and refuses turns, toggle on restores it;
actions are delivered (the destructive allow-list is printed); the Actions
switch changes the state within 60 seconds and is restored.

## Evaluation set (14.5)

```bash
python3 run_eval.py eval --api "$ASSISTANT_API_URL" --portal-api "$API_URL" --usecase-id "$USECASE_ID" --device-id "$DEVICE_ID" --out results.json
```

`cases.json` holds 26 scripted prompts with expected outcomes: guide
citations, `journey_status`, the read-only tool the prompt calls for, no
confirmation for a deployment or a deletion (destructive tools are not on
the assistant's allow-list), an off-topic decline, an "I don't know",
prompt-injection resistance, role awareness, and four action cases: a
confirmed change followed by its status check, a rejected change, two
changes in one prompt with one card each (the first confirmed, the second
rejected), and a card left to expire, which ends the turn and refuses the
late confirmation. The expiry case waits out the binding's lifetime (10
minutes by default). Without `--portal-api` the action cases are skipped.
Each case gets up to two attempts (model output varies); the run passes at
90 % or more. Copy `results.json` into the spec's `verification-notes.md`.

`--verbose` adds the response text to the results file; use it only for a
local look and do not commit the output.
