# Workflow telemetry dashboards

Charts, "Now" and "Health" panels, a clip gallery and a live frame of a
deployed workflow, rendered by the Portal from the periodic summaries the
devices publish. Nothing a dashboard does needs a Greengrass deployment: the
devices learn that telemetry is on, how often to summarize and whether a live
frame is wanted through a named device shadow.

The dashboards are **not** the debug mode (`docs/workflow-debug-mode.md`). The
dashboards show summaries of every workflow of a use case; the debug mode sends
the full record of every run of one workflow to its own log for a limited
time. The two share the device-side publisher and the topic convention and
nothing else.

## What the dashboards show and where the data comes from

- Every **10 seconds** (the use case's summary interval, 5-60 s) the
  LocalServer on each device folds the runs of each deployed workflow into one
  `dda.summary.v1` message: run counts (completed, failed, dropped), latency
  (p50, p95, max), detections per class, people seen, the model state and its
  readiness waits, the continuous-runner state and the telemetry counters
  (queue drops, publish failures, events dropped). When a workflow has not run
  for five minutes the device sends a small Heartbeat every 60 s (30-300 s)
  instead, so "silent" and "idle" stay distinguishable.
- **Events** (`dda.event.v1`) are published the moment they happen: a confirmed
  violation, a ready clip, a run error (coalesced to at most one message per
  error per 10 s).
- **Live frames** (`dda.live.v1`) exist only while a viewer holds a lease on a
  dashboard's device: the device uploads at most one downscaled JPEG per second
  to the use case's results bucket and announces it.
- There are **no per-frame messages**: a device publishes about six summaries a
  minute per workflow, not one message per inference.

One IoT rule per account forwards the messages to a Portal function that
validates them against the committed schema registry, attributes them to a
device by the publishing certificate and to a use case by the device's record,
keeps the newest values and the minute, hour and day aggregates in DynamoDB,
and writes every accepted record to an S3 data lake (`raw/summaries/` and
`raw/events/`, gzip JSON, one folder per day, kept 400 days). The Portal reads
only the DynamoDB tables; the lake is kept for later analysis.

**No Quick Sight in this phase.** The owner redirected the first design to
Portal-native charts because the per-message path would have cost about forty
times more at fleet scale and because the Quick Sight edition change cannot be
reversed. The lake keeps that door open: a later add-on can define Glue tables
over `raw/summaries/` and `raw/events/` with partition projection by day, two
de-duplicating views (the lake is at-least-once; a Summary is unique by
producer, agent instance, workflow and `seq`), an Athena workgroup with a
scan cap, and either Standard-edition analyses for the account's existing
Quick Sight administrators or, with the Enterprise edition, embedded
dashboards with row-level security per use case. None of it is built or paid
for now; the design's section 16 describes the path.

## Who can do what

| Action | Permission | Roles |
|---|---|---|
| Open the dashboards list, a dashboard and its tiles | `dashboards:read` | Viewer, Operator, DataScientist, UseCaseAdmin, PortalAdmin |
| Create, rename, re-device and delete a dashboard | `dashboards:manage` | Operator, DataScientist, UseCaseAdmin, PortalAdmin |
| Enable telemetry for a use case, tune it, read the rejection counters | `telemetry:manage` | UseCaseAdmin, PortalAdmin |
| Read the account-wide rejection counters | PortalAdmin | PortalAdmin only |

DataLabeler holds none of them. The Portal hides controls a role cannot use
and the API checks the permission on every call, audits denials, and audits
dashboard creation, update and deletion, telemetry enablement and settings
changes, the first grant of a live lease and the first minting of a clip's
URLs per viewer and hour.

## Where to find it

- **Workflow dashboards** in the side navigation (after **Deployments**) lists
  the dashboards of a use case and carries **Create dashboard**
  (`/workflow-dashboards`).
- A dashboard's name opens it (`/workflow-dashboards/<dashboard>`).
- **Telemetry settings** (on the list and on a dashboard, for UseCaseAdmins and
  PortalAdmins) opens the use case's settings and readiness checklist
  (`/telemetry-settings/<use case>`).

## Enabling telemetry and reading the checklist

Telemetry is **off** for every use case until a UseCaseAdmin or PortalAdmin
switches it on. The settings page holds three values and a read-only media
location:

| Setting | Values | Default |
|---|---|---|
| Telemetry enabled | on / off | off |
| Summary interval | 5-60 s | 10 |
| Idle heartbeat | 30-300 s | 60 |
| Media location | the use case's results bucket under `dda-telemetry/`, with the bucket's region and where the region came from (`header`: the bucket's own answer; `usecase`: the use case's region, when the bucket did not say) | read-only |

Saving writes the settings and then the telemetry shadow of every device that
has a record for the use case; the answer lists each device's result (`ok` or
`failed` with the reason) and the devices Greengrass knows for the use case
that have **no** record, which publish nothing. Above 40 devices the answer is
"accepted" and the maintenance function finishes the shadow writes within five
minutes. Enabling is refused when the use case is outside the Portal's region,
when a cross-account use case's role is not a `DDAPortalAccessRole`, or when the
media location cannot be reached.

The **readiness checklist** below the form shows one line per item with its
status and, when it is not ready, what to do:

| Item | Ready when | What to do when it is not |
|---|---|---|
| Ingestion resources | Portal-account use cases: always. Cross-account: the account's telemetry rule exists and points at the Portal's ingest alias for that account. | Update the use-case account template (`DDAPortalUseCaseAccountStack`) in the use case's account to the version with telemetry. |
| Region | the use case is in the Portal's region | Telemetry is not supported for use cases outside the Portal's region. |
| Role form (cross-account only) | the role ARN is an IAM role of the use case's account | Register the use case with its account's `DDAPortalAccessRole` ARN. |
| Device records | every managed core device of the use case has a device record naming this use case; the item lists the ones that do not and says when a record names another use case | A UseCaseAdmin or PortalAdmin sets the device's **Target architecture** on its device page (Devices, then the device), which calls `PUT /devices/{id}` with `{"usecase_id": "<this use case>", "target_architecture": "<arch>"}`; `{"usecase_id": ..., "test_device": false}` also works; or re-run quick setup on the device. The route needs `node_designer:manage`, which Operators do not hold. Until then the device publishes nothing and cannot be added to a dashboard. |
| A telemetry source | a summary of the use case has arrived | Deploy the LocalServer release that carries the telemetry agent to a device of this use case; summaries appear within a minute of enabling. |
| Media location | the Portal can list the use case's results bucket under `dda-telemetry/` | When the bucket does not exist: create the use case's inference-results bucket, or set its override (nothing in the repository creates the default bucket). When the Portal is refused: an override bucket is read only when it is tagged `dda-portal:managed=true` (cross-account) or once the data-plane grant is wired (Portal account); the default bucket needs the use-case account template with telemetry. |
| Media retention | the bucket carries the `DDATelemetryClips` and `DDATelemetryLive` lifecycle rules and versioning is not enabled | Run `scripts/telemetry_lifecycle.py --bucket <name> --clip-days <n> --apply` with operator credentials. |
| Registry enforcement | always ready; a **warning** while enforcement is off | "Registry enforcement is off: every signed-in user holds `dashboards:read` on this use case, so clip playback and live frames are refused until it is on." |
| Device shadows | every recorded device accepted the current settings | The listed devices did not accept the telemetry shadow: check that they are online. |

Two warnings appear above the list when they apply: the **cross-region
warning** ("the bucket is in `<region>` while the use case is registered in
`<region>`: devices PUT across regions, with transfer cost and latency", or
"region taken from the use case (header absent); verify the bucket's region")
and the **enforcement warning** quoted above.

## Creating a dashboard

**Create dashboard** is a five-step wizard: the use case; one to twenty of its
devices (only devices with a record for the use case are offered; a device
that lost its record is refused by the API with the device-record remedy
above); a workflow of the use case; a template, shown with its availability
for that workflow on those devices (`available`, `partial` with the panels no
summary of the last 24 hours reported, or `unavailable`); and a name, unique
within the use case. There is no AWS console step and nothing is deployed:
creating the dashboard writes the chosen devices' telemetry shadows so they
know the use case and the person classes, and the first summaries appear
within a minute when telemetry is on. A use case holds at most 100 dashboards.

## The three templates

A template is a committed JSON layout; the Portal renders any template the API
serves, so a new one is a file plus a test.

| Template | Panels | Tiles | Needs from the workflow |
|---|---|---|---|
| Generic detector | Runs per minute (completed, failed, dropped); Latency (p50, p95); Detections per class (stacked); People seen; Model state (one band per state, see below); Model waits (readiness waits as bars, waited time as a line) | Now, Health, Live | run counts and detections |
| Anomaly detection | Runs per minute; Anomalous share (a pie of the range and a line over time); Latency; Model state | Now, Health, Live | run counts and outcomes |
| PPE compliance | the Generic detector panels plus Violations by reason (stacked); People compliant and violating; Clips per hour; Violation events | Now, Health, Clips, Live | run counts, detections and the `ppe` extension the clip helper publishes |

The **Model state** panel is a stacked bar chart with one series per model
state (`READY`, `UNLOADING`, `LOADING`, `INCOMPLETE`, `NOT_DEPLOYED`, `UNKNOWN`,
`UNAVAILABLE`); each bucket contributes one unit to the band of its worst
state, so a healthy hour is a flat `READY` band and a loading or unknown
episode shows as a colour change. `NONE` (no Triton model in the workflow) is
not drawn. A panel whose measures no summary of the last 24 hours reported
reads "not reported by this workflow" instead of an empty chart.

The charts read one series answer per dashboard per refresh, fold it across
the dashboard's devices (sums for counts, maxima for maxima, weighted means for
latencies, the worst state for the model) and offer a per-device drill-down
from the same answer. The range picker offers the last 15 minutes, 1 hour, 6
hours, 24 hours, 7 days and 30 days, and absolute ranges up to 400 days.

## The tiles and their freshness

| Tile | Shows | Refresh | Typical age of a value |
|---|---|---|---|
| Charts | the panels above at minute detail up to 6 hours, hourly up to 60 days, daily beyond | every 60 s at minute detail, 300 s at hourly, 900 s at daily, while the page is visible; a manual refresh at most once per 10 s | one summary interval plus one refresh |
| Now | per device: flow (`flowing`, `idle`, `silent`), "last summary" and "last run" ages ("no run in 48 h" when there was none), the heartbeat age, the newest values (runs, detections per class, people, latency, model), the live-frame age, the 20 newest events of the last 15 minutes | every 5 s while visible (`telemetryNowPollSeconds`) | about 10 s typically, 17 s at the 95th percentile |
| Health | per device: flow, sequence gaps (Summaries or Heartbeats of the workflow that never arrived; the device numbers each workflow's messages from 1 at every agent start) and the telemetry counters of the last hour, failed and dropped runs, the model state with its readiness waits, the Greengrass core-device status, the variant hint (below); for `telemetry:manage` holders the use case's rejection counts of the last hour | with Now (one request feeds both panels) | as Now |
| Clips (PPE template) | 25 clips per page, newest first, with a device filter; each clip is `playable`, "upload failed on the device" (with the device's error text), "expired or missing", "not allowed" (its reference is outside the use case's media layout) or "unavailable"; Play opens a `<video>` on a 15-minute URL minted on demand, with a download link | every 15 s while visible | a clip is listed within 30 s of its event |
| Live | one selected device: the newest frame with its per-class counts, people and PPE state; `starting` ("the device is not sending" after 15 s), `live` (at most 10 s old), `stale`, `paused`, `unavailable` with the reason, or "no live source" | the lease is renewed every 20 s and the frame polled every 2 s while visible and unpaused | about 3.5 s from capture to screen |
| Events | the dashboard's events newest first, 50 per page, with device and type filters | every 15 s while visible | within a few seconds of the event |

The Now and Health values always come from the newest **summary**; a
heartbeat moves the flow to `idle` and the ages, never the values. Live
viewing is leased: five concurrent viewers per use case by default, eight
leased workflows per device, 30 continuous minutes per viewer before the tile
pauses and offers **Resume**; a lease that cannot be granted reads "No live
slot is available" and the tile retries every 20 s. The live image is always
the Portal-signed S3 address of the frame; the Portal never shows a
device-local address.

## Registry enforcement: the two switches

Telemetry has two switches: the use case's **telemetry enabled** setting and
the Portal-wide **registry enforcement** (`PORTAL_REGISTRY_ENFORCED`). While
enforcement is off, every signed-in user holds `dashboards:read` on every use
case, so the API refuses the three routes that mint media URLs with
`registry_enforcement_required` and the Portal shows that refusal as a notice
in place of the tile it concerns.

| Panel | Enforcement on | Enforcement off (the default) |
|---|---|---|
| Charts (runs, latency, detections, violations, people) | yes | yes |
| Now and Health | yes | yes |
| Events list | yes | yes |
| Clip gallery | listing and playback | listing only |
| Live tile | yes | no (`registry_enforcement_required`) |

Plan to turn enforcement on before clip playback and live tiles are expected;
the checklist's "Registry enforcement" item warns while it is off.

## Caps, deployment values and cost

| Value | Default | Range | Where it is enforced |
|---|---|---|---|
| Summary interval (per use case) | 10 s | 5-60 | the settings; the device clamps |
| Idle heartbeat (per use case) | 60 s | 30-300 | the same |
| `telemetryNowPollSeconds` | 5 | 5-30 | the Portal reads it from `/telemetry/schemas` |
| `telemetryLiveMaxLeases` (per use case) | 5 | 1-20 | the lease slots |
| `telemetryLiveMaxViewMinutes` | 30 | 5-240 | the lease |
| `telemetryClipRetentionDays` | 90 | 7-365 | the clip index and the lifecycle rule |
| `telemetryLakeRetentionDays` | 400 | 30-3650 | the lake's lifecycle rule |
| `telemetryIngestConcurrency` | 50 | 10-500 | the ingest function's reserved concurrency |
| Devices per dashboard | 20 | fixed | validation |
| Dashboards per use case | 100 | fixed | a counter |
| Summary and event size | 16 KB | fixed | the device and the schema |
| Events per minute | 60 per device (the device); 120 per device and workflow (the ingest) | fixed | the device's rate limiter; the ingest rejects the excess |
| Devices written synchronously by one settings save | 40 | fixed | above it the save is "accepted" and finished by maintenance |
| Per-class labels in a summary | 64 | fixed | the device folds the rest into `__other__` |
| Live frame | 1280 x 720, 300 KB, at most 2 fps (1 by default) | fixed | the lease and a size check before the URL is minted |
| Chart refresh by detail | 60 s / 300 s / 900 s, one request per dashboard per refresh | fixed | the Portal |

The CDK stack checks every range when it synthesizes. **Cost, in brief**
(us-east-1 list prices read in October 2026): about **six dollars a month** at
demo scale (one device and workflow, eight hours a day, 130 viewer-hours of
tiles and 44 of live frames) and about **330 dollars a month** at a fleet
scale of 300 device-workflow pairs over ten hours a day with 2,200 viewer-hours
of tiles and of live frames; roughly a tenth of that is IoT messaging, the
largest single fleet line is live viewing (about five cents per viewer-hour,
bounded by the leases), and the two DynamoDB lines together stay well under
what per-frame writes would have cost. Halving the summary interval doubles
the message, write and roll-up lines.

## The `amd64Nvidia` variant

All seven LocalServer recipes grant the component the IPC publish permission
the telemetry agent needs. The `amd64Nvidia` recipe gained it with the trunk
integration, and that variant's device verification is pending with its first
build. A device of that variant is recorded with the target architecture
`x86_64_nvidia`; when its publish failures climb while its flow is `silent`,
the Health panel still shows the variant hint, which now points at a device
running a LocalServer built before the recipe change (redeploy a current
build), and the device page shows the same target architecture. No other
variant produces the hint.

## PPE demo onboarding

The PPE demo pair has two producers: the LocalServer telemetry agent
(`engine`), which fills the run, latency, detection, people and model blocks,
and the clip helper (`ppe-clipper`), which fills the `ppe` extension
(compliant and violating people, violation frames by reason, clips opened) and
publishes the `clip_ready` and `violation_confirmed` events once it adopts the
contracts. The two producers' blocks are folded by owner, so nothing is counted
twice. The demo's existing topics and the workflow's `mqtt_publish` node are
untouched.

| Step | What the PPE dashboard shows |
|---|---|
| The Portal is deployed and telemetry is enabled for the demo use case; no device release yet | "A telemetry source" is not ready on the checklist; the panels are empty. |
| The LocalServer release with the agent is deployed to the demo device (owner-gated, one deployment) | Runs per minute, failed and dropped runs, latency, detections per class, people seen, the model state, Now and Health, and the live tile under a lease once enforcement is on. |
| The clip helper adopts the contracts (demo tooling, outside this repository) | Violations by reason, compliant and violating people, clips per hour, the clip gallery with playback, and the violation events. |

The helper's contract, field by field (its summary with only the `ppe`
extension, the `clip_ready` event with its revision rule and the stable clip
identity, the `violation_confirmed` event, the media layout under
`dda-telemetry/clips/`), is section 13 of the design in
`.kiro/specs/workflow-telemetry-dashboards/design.md`. A throwaway test clip
under that layout is played before the helper publishes real ones.

## Shared accounts: what stays open

Two limits remain when several use cases share one AWS account, accepted for
the lab and to be revisited before a production use case whose clips show
people:

- Every device of an account runs under one device role that may read and
  write every object under `dda-inference-results-*`, so a device of one use
  case can read or replace another use case's clips and live frames in the
  same account. The sequence and checksum checks catch accidents, not intent;
  the only isolation is a separate account per use case.
- Any UseCaseAdmin can re-point a device record to another use case through
  the device update route; the device's telemetry shadow follows the record on
  the next settings write or re-sync, and the original use case loses that
  device's telemetry until the record is set back.

The Portal never exposes a public object or URL: clip and frame URLs are minted
per request, after the permission check and the enforcement guard, for 15
minutes and 120 seconds respectively, and are never logged.

## Rollout (owner-gated runbook)

Nothing below has been done by the implementation; each step is a separate,
owner-approved action.

- **RB-1, read-only pre-checks:** the demo device's publishing identity, the lab
  use cases' topology and role form, the enforcement state, the REST API's
  resource count against its limit (the telemetry routes add 18 resources, the
  debug mode's about 10), the REST API's authorizer count against its limit
  (`aws apigateway get-authorizers`: existing plus new must stay within 10,
  a quota only AWS Support raises; the first deploy attempt rolled back on it
  when each new stack still created its own authorizer, so both now attach to
  the portal API's existing authorizer and add none, and the deploy passes its
  id as the CDK context value `sharedAuthorizerId`), the account quotas, the
  results bucket's existence
  and versioning and whether it reports its region, and the two device-side
  prerequisites: the devices' IoT policy allows publishing on the telemetry
  and debug topics, and the component access policy is attached to the
  devices' token-exchange role. A gap in the IoT policy would disconnect the
  whole core device on the first publish, so RB-1 stops when either check
  fails; the remedy is an account-side policy change, never a recipe change.
- **RB-2, Portal deploy:** the infrastructure, then the frontend; never during
  a component build. `deploy-infrastructure.sh` resolves the shared authorizer
  id and passes it as `-c sharedAuthorizerId=<id>`; without it the CDK app
  skips the two new stacks with a warning and deploys every other stack as
  before (a fresh install runs the script twice).
- **RB-3, the results bucket:** create or confirm the lab bucket and apply the
  lifecycle rules with `scripts/telemetry_lifecycle.py`.
- **RB-4, rollback:** destroy the telemetry stack and re-create the API
  deployment; the retained deployment keeps the stage valid.
- **RB-5, the use-case account template** in any cross-account use case.
- **RB-6, enable telemetry** for the demo use case and confirm the checklist.
- **RB-7, the LocalServer build and device phase:** build the JetPack 5
  LocalServer with the agent (one build at a time), deploy it to the demo
  device, verify that summaries, heartbeats and the end of a superseded
  registration behave as designed, measure the overhead and the device's
  uplink and spooler settings, then commit the edge code with the record.
- **RB-8, end-to-end verification:** attribution, Now and Health, charts after
  two minutes, a lease and a live frame, a throwaway test clip, an empty rule
  error log, no access denials for the new roles.
- **RB-9, the helper's adoption** of the contracts (demo tooling, outside the
  repository).
