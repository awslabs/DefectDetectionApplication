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
  `AssistantApiUrl`) and, for the audit check, the portal API URL
  (`EdgeCVPortalComputeStack` output `ApiUrl`).

## Smoke check (14.6)

```bash
python3 run_eval.py smoke --api "$ASSISTANT_API_URL" --portal-api "$API_URL" --usecase-id "$USECASE_ID"
```

Checks: state enabled; a guidance turn completes and cites the guide; a
read-only tool turn completes and ran a read-only tool; the Audit Logs
`channel=assistant` filter shows the two turns' entries; Settings toggle off
hides the assistant and refuses turns, toggle on restores it; actions are
not delivered in this release. (The rejected-confirmation step is a Phase 2
check: no mutating tool is exposed in Phase 1.)

## Evaluation set (14.5)

```bash
python3 run_eval.py eval --api "$ASSISTANT_API_URL" --usecase-id "$USECASE_ID" --device-id "$DEVICE_ID" --out results.json
```

`cases.json` holds 22 scripted prompts with expected outcomes: guide
citations, `journey_status`, the read-only tool the prompt calls for,
refusal of state changes without a confirmation, an off-topic decline, an
"I don't know", prompt-injection resistance, and role awareness. Each case
gets up to two attempts (model output varies); the run passes at 90 % or
more. Copy `results.json` into the spec's `verification-notes.md`.

`--verbose` adds the response text to the results file; use it only for a
local look and do not commit the output.
