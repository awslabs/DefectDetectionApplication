# Workflow debug mode

Switch a misbehaving workflow into debug mode for a limited time and read every
run it makes, with its trigger, nodes, detections, bindings, error and the log
lines that belong to it, in a log you can tail and download from the Portal.

The debug mode is **not** part of the telemetry dashboards. The dashboards show
periodic summaries of every workflow; the debug mode sends the full record of
every run of **one** workflow on the devices you choose, into its own log, and
switches itself off when the time is up. Nothing you start here changes a
Greengrass deployment: the device learns about a session through a named
device shadow.

## What it is and what it is not

- **It is** a time-boxed, per-workflow, per-device stream of full-fidelity run
  records written to a CloudWatch Logs group owned by the Portal, one group per
  use case, one stream per device and workflow.
- **It is not** shown on the dashboards, it is not a replacement for the device
  logs (the component logs the LogManager ships), and it does not change what
  the workflow does: `run.log` on the device is the same with or without a
  session.
- Images are off by default. A session can upload one small image per run
  only when registry enforcement is on (see the two-switch matrix below) and
  the results bucket carries the debug-images lifecycle rule.

## Who can do what

| Action | Permission | Roles |
|---|---|---|
| Start and stop a session | `MANAGE_DEVICES` | Operator, UseCaseAdmin, PortalAdmin |
| List sessions, tail, filter and download the log | `VIEW_DEVICE_LOGS` | every role except DataLabeler |
| Open the CloudWatch console deep link | PortalAdmin | PortalAdmin only |

The Portal hides controls a role cannot use; the API checks the permission on
every call and audits denials. Starting, stopping and the first read of a
session by a user within an hour are audited (`debug_session_started`,
`debug_session_stopped`, `debug_session_expired`, `debug_log_viewed`).

## Where to find it

- **Debug log** in the side navigation lists the sessions of a use case and
  carries the **Start debug** action (`/debug/<use case>`).
- The **Debug** tab of a device page lists the sessions covering that device
  and starts one with the device preselected.
- A device name in either table opens the viewer for that device and
  workflow (`/debug/<use case>/<workflow>/<device>`).

## Starting and stopping a session

**Start debug** asks for:

| Field | Values | Default |
|---|---|---|
| Workflow | a workflow of the use case | — |
| Devices | one device, or every device that has a deployment of the workflow and a device record in this use case | one device |
| Duration | 5–1440 minutes | 60 |
| Level | `debug` (adds the engine's DEBUG lines to each run record) or `info` | `debug` |
| Images | one image per run, 50–300 KB each | off |

The API refuses, with a remedy in the answer:

- a device without a device record naming this use case (`device_not_recorded`):
  a UseCaseAdmin or PortalAdmin sets the device's **Target architecture** on
  its device page, or quick setup is re-run on the device;
- "all devices" that resolves to nothing (`no_target_devices`): the answer lists
  the devices without a deployment and the devices without a record;
- a cap reached (`debug_session_limit`: sessions per use case, sessions per
  device, devices per session) or a live session already covering the same
  device and workflow (`debug_session_exists`);
- images while registry enforcement is off (`registry_enforcement_required`)
  or while the results bucket lacks the `DDATelemetryDebugImages` lifecycle
  rule (`debug_images_unavailable`: run `edge-cv-portal/scripts/telemetry_lifecycle.py --bucket <name> --apply`).

Creation is all-or-nothing: either the session exists with every device, or
nothing was created.

**Stop** removes the session from every device and marks it `stopped`. A
session that reaches its end is marked `expired` by a maintenance run that
executes every five minutes; that run also retries the shadow writes of a
`pending` session and recreates a missing log group.

### What the device does

The Portal writes one entry per session and workflow into the device's named
shadow `dda-workflow-debug`. The device's debug agent reads the complete
desired document (never a delta alone), validates the entry and applies it:

- `pending` until the workflow is registered on the device, then `active`;
  `no_registration` when no registration appears within 30 s; `rejected`
  when the entry is invalid (the reason is reported);
- `expired` when `expiresAt` passes, `stopped` when the entry disappears from
  the document. An early stop reaches the device within about two seconds.

**The time counts from the moment the Portal started the session.** A device
that was offline for 50 of 60 minutes gets the remaining 10 minutes, not a
fresh hour. A LocalServer restart resumes a session that is still in the
future (the first record says `resumed`); nothing already written is lost or
duplicated.

The device reports its view of each session (`pending`, `active`, `expired`,
`stopped`, `no_registration`, `rejected`) with its counters; the sessions
table shows it beside the Portal's own state and the shadow write result.

### Session states on the Portal

`pending` (created; at least one device has not accepted the shadow yet) →
`active` (every device accepted) → `stopped` (you stopped it) or `expired`
(time up). `pending` and `active` count against the caps and accept records;
`stopped` and `expired` do neither. History stays visible for 30 days.

## What a run record contains

One `run` record per finished run of the workflow on the device:

- **status** (`completed` or `failed`), the **execution id** and the timing row
  **queued / setup (readiness wait) / pipeline**: how long the run waited in
  the queue, how long the executor spent before the pipeline started
  (including the wait for a model to become ready) and how long the pipeline
  itself took;
- the **trigger** that started it: `mqtt` (topic and payload), `opcua`
  (endpoint and node, value), `continuous` (frame sequence and times),
  `manual`, or `unknown` for a trigger kind the record format does not know
  yet, in which case the whole trigger context is shown serialized;
- the **nodes** with status, duration and detail, the **detections** (label,
  confidence, box), the LLM and Bedrock outputs, the anomaly verdict, the
  **bindings** (outputs sent to MQTT, OPC UA, Modbus and the like with their
  result), the **error** and **traceback** of a failed run and the failing
  node;
- the **model** readiness the run waited for, the **frame source** (camera,
  size, frame number) and the **artifacts** (output directory, capture id,
  overlay);
- the run's **logs**: the lines of `run.log` (`src: runlog`) merged with the
  lines the device's log tap attributed to the run that `run.log` never
  captures (`src: tap`: the engine's DEBUG lines during a `debug` session and
  every model-server line), ordered by time;
- the **image** when the session uploads images (loaded on demand through a
  short-lived link);
- **truncated** markers naming each field that was cut to fit the message
  budget and its original size.

Beside the run records the log carries `log` records (lines between runs:
trigger runtime, model readiness, continuous waits), `drop` records (frames
skipped or dropped, coalesced per reason per second) and `session` records
(start, a heartbeat every 30 s with the counters, resume, stop, expiry).

Secrets are redacted on the device before anything is sent, and the ingest
redacts again.

## Caps and why they exist

| Cap | Value |
|---|---|
| Messages per second per device | 10 |
| Bytes per second per device | 256 KB/s |
| Message size | 120 KB |
| Records per message | 200 |
| Queue on the device | 500 records, oldest dropped first |
| Session duration | 60 minutes by default, 5 minutes to 24 hours |
| Live sessions per use case | 4 (a multi-device session counts once) |
| Live sessions per device | 2 |
| Devices per session | 20 |
| Tap buffer per open run | 500 lines, 64 KB |
| Images | 960 × 540, 150 KB (50–300), 2 per second, 2,000 per session |
| Log retention | 14 days |

**Why the byte cap:** every component on a Greengrass core device shares one
MQTT connection and one in-memory spooler (2.5 MB by default). A debug
session that published as fast as it could would fill that spooler and
starve the other components' messages, and it could saturate a device whose
uplink is only a few Mbps. The byte bucket keeps a session well under both;
when the spooler is full anyway, the agent backs off and counts it
(`spoolFull`) instead of retrying blindly.

**Why the record caps:** a run record is cut to fit 120 KB in a fixed order
(tap lines first, then `run.log` lines, detections beyond 100, long texts,
the payload, the traceback, details, the error), and each cut is listed in
`truncated`, so what you see is always honest about what is missing. The
complete `run.log` stays on the device.

## Registry enforcement: the two switches

`PORTAL_REGISTRY_ENFORCED` is a Portal deployment switch. While it is **off**
(the default until the identity registry has been backfilled), every signed-in
user resolves to at least Viewer on every use case and therefore holds
`VIEW_DEVICE_LOGS` everywhere, exactly as the existing device-logs feature
already allows.

| Page element | Enforcement on | Enforcement off |
|---|---|---|
| Sessions list, session detail, start and stop (`MANAGE_DEVICES`) | yes | yes |
| Debug log tail, filters, download (`VIEW_DEVICE_LOGS`) | yes | yes, with the warning "Registry enforcement is off: every signed-in user can read this log" |
| Images (`images` at start; the image link in a run card) | yes | no: the API answers 409 `registry_enforcement_required`; the Start debug modal says so and keeps the images control disabled |
| Console deep link | PortalAdmin | PortalAdmin |

The frontend learns the switch from its deployed `config.json`
(`registryEnforced`, absent means off, the same default as the backend). The
server decides regardless of what the page shows.

## The `amd64Nvidia` variant

The `amd64Nvidia` LocalServer recipe carries the IPC statement that
publishing on the debug topic needs since the trunk integration; that
variant's device verification is pending with its first build. A device still
running a LocalServer built before that change behaves as before: the session
becomes `active` (the shadow read and the reported writes work), but every
record publish is denied, so the stream stays empty while the device's
`publishFailures` counter climbs. The device record for that hardware says
`target_architecture: x86_64_nvidia`; the sessions table shows a notice
naming the device when its record says so and its publish failures are above
zero, and the remedy is to redeploy a current build.

## The viewer

The viewer tails the device's stream for the chosen window of **write time**
(when the Portal wrote the record), ordered by each record's own time:

- the default is the last 15 minutes with the tail on; the tail polls every
  3 s while the tab is visible and stops while it is hidden;
- each poll re-reads a 60 s overlap and shows each record exactly once; a
  record written long after the device sent it (a retried ingest) appears at
  its place in time with a **late** badge;
- a poll reads at most 3 pages of 2,000 events; a longer backlog is shown as
  **catching up** and continues on the next poll;
- when the API rate-limits reads, the toolbar says "Log reads are
  rate-limited; polling every 10 s" and the tail returns to 3 s after two
  clean polls;
- the filters are execution id, node id, level (`DEBUG`, `INFO`, `WARNING`,
  `ERROR`) and record type (`run`, `log`, `drop`, `session`); a window spans
  at most 24 hours;
- **Download NDJSON** reads the chosen window in pages of 10,000 events at no
  more than 2 calls per second, pausing when rate-limited, and assembles one
  JSON event per line in the browser, up to 50,000 events (narrow the window
  for more);
- the body renders the last 2,000 events of the buffer; the download is the
  tool for larger volumes.

Each event carries the record, its stable `id`, the time the Portal wrote it
(`writeTs`) and the time IoT received the message (`receivedMs`).

## The console deep link

A PortalAdmin can open the device's stream in the CloudWatch Logs console
(**Open in CloudWatch** in the viewer). The console's timestamp column is the
write time; the record's own time and the receipt time are inside each event.

## Deployment values

Set as CDK context on the Portal stacks (`-c <key>=<value>`); each is
range-checked at synth.

| Key | Default | Range | What it sets |
|---|---|---|---|
| `debugLogRetentionDays` | 14 | 1–90 | retention of the debug log groups |
| `debugMaxSessionsPerUsecase` | 4 | 1–20 | live sessions per use case |
| `debugMaxDevicesPerSession` | 20 | 1–32 | devices per session (bounded by the all-or-nothing transaction) |
| `debugMaxMessagesPerSecond` | 10 | 1–20 | the device's message bucket |
| `debugMaxBytesPerSecond` | 262144 | 32768–1048576 | the device's byte bucket (32 KB/s to 1 MB/s) |
| `debugIngestConcurrency` | 20 | 10–200 | reserved concurrency of the ingest function (about 100 messages/s at 20) |
| `debugGetLogEventsPerSecond` | 4 | 1–8 | the API's read pacing for the plain tail |
| `debugFilterLogEventsPerSecond` | 2 | 1–4 | the API's read pacing for an execution-id filter |

The read pacing exists because CloudWatch Logs read quotas are per account
and Region and are shared with every other Portal feature that reads logs
(build logs, device logs); the defaults fit under the smallest Regional quota
together with those readers. An account in a larger-quota Region may raise
them.

Two more switches live elsewhere: `PORTAL_REGISTRY_ENFORCED` (the Portal
handlers' context value `portalRegistryEnforced`) and the frontend's
`config.json` key `registryEnforced`, which should mirror it.

## Rollout (owner-gated runbook)

Everything below is performed by the owner, in order, outside the automated
delivery:

- **RB-1, read-only checks.** The device's certificate principal matches what
  the ingest will see; the demo use case's account topology and role form;
  the demo device has a device record for the use case (if not, set its
  Target architecture first) and its recorded architecture is not
  `x86_64_nvidia`; the REST API still has room for the new routes; the REST
  API's authorizer count (`aws apigateway get-authorizers`: existing plus new
  must stay within 10, a quota only AWS Support raises; the first deploy
  attempt rolled back on it when this stack still created its own authorizer,
  so the stack now attaches its routes to the portal API's existing authorizer
  and adds none, and the deploy passes its id as the CDK context value
  `sharedAuthorizerId`); the
  Region's `GetLogEvents` and `FilterLogEvents` quotas, so the read-pacing
  values are set knowingly; who holds `MANAGE_DEVICES`; the registry
  enforcement state (images need it on); the results bucket answers with its
  Region header when images will be used; the core device's IoT policy allows
  publishing on the debug topic (an unauthorized publish would disconnect the
  whole core device, not drop one message); the device's token-exchange role
  carries the Portal component policy for the image upload.
- **RB-2, Portal deploy.** Deploy the Portal stacks and the frontend, never
  during a component build. `deploy-infrastructure.sh` resolves the shared
  authorizer id and passes it as `-c sharedAuthorizerId=<id>`; without it the
  CDK app skips the two new stacks with a warning and deploys every other
  stack as before. On a fresh install, where that deploy creates the API, the
  script then looks the id up again and deploys the two stacks in a second
  pass. Set `registryEnforced` in the frontend config to match the
  enforcement state.
- **RB-3, use-case template.** Update the use-case account template in any
  cross-account use case; this also lands the shared telemetry read
  statements on the Portal access role.
- **RB-4, rollback.** Destroy the debug stack, then create a new API
  deployment so the stage stays valid.
- **RB-5, lifecycle rule.** If images will be used, apply the
  `DDATelemetryDebugImages` rule with the lifecycle script.
- **RB-6, a first session before the device release.** Start a 10-minute
  `info` session for the demo device and workflow; expect the shadow write to
  succeed and the session to go `pending` → `active` on the Portal side, with
  no device report yet (the agent is not on the device).
- **RB-7, the LocalServer release.** Build the LocalServer with the agents
  (one build at a time, gate green), deploy it to the demo device, record the
  device's measured uplink and the nucleus's effective spooler size, choose
  the session's message and byte caps below both, start a 60-minute `debug`
  session, confirm records within 10 s and that `run.log` is byte-identical
  with and without a session, measure the overhead, stop early and confirm
  the device stops within 2 s, restart LocalServer during a second session
  and confirm the resumed records are not dropped, then commit the edge code
  with the verification recorded.
- **RB-8, end to end.** Attribution, `session_inactive` after expiry, the
  redaction of a planted test secret in a trigger payload, the viewer showing
  each event exactly once across a 10-minute watch including one deliberately
  late write with its badge, a model-server line inside a run record's logs
  with `src: tap`, the console link, an empty rule-errors log group and no
  access denials in CloudTrail.

Until RB-7 is done, the edge (LocalServer) code of this feature stays
uncommitted in its worktree, as the build policy requires on-device
verification before an edge change is committed.
