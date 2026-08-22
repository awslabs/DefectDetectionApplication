# On-Device Verification Runbook: JSON Trigger → Metadata Pipeline

This runbook verifies, on real edge hardware, that metadata from an MQTT JSON
trigger payload reaches the Custom Python source node, that the node extracts
an image from the payload (URI reference or embedded base64), that the frame
reaches model inference, and that the published result carries the inference
output plus the echoed correlation metadata — all over the production
Greengrass transport.

Run it on every architecture the change touches (JP5 and/or JP6 and/or JP7),
per the workspace `builds.md` on-device verification rule. Record what was
verified on which device in the commit/PR.

## Pipeline under verification

```
mqtt_subscribe ──► custom_python_source ──► model_inference ──► metadata ──► mqtt_publish
 (trigger topic)    (dual-path handler)      (deployed model)    (correlation)  (result topic)
```

Assets in this spec directory:

| Asset | Purpose |
|---|---|
| `runbook/workflow.json` | The ready-to-import Pipeline_Workflow definition (schemaVersion 1). The handler code is inline in the `custom_python_source` node. |
| `runbook/handler.py` | Reference copy of the dual-path Handler_Script — identical in logic to the code inline in `workflow.json` and to the automated integration test's handler. |

## Transport and topics

Both directions use the **Greengrass transport**: the LocalServer backend
reaches AWS IoT Core through the Greengrass Nucleus over the
`aws.greengrass.ipc.mqttproxy` IPC service (both trigger nodes in
`workflow.json` set `"greengrass": true`).

| Direction | Node | Exact topic string | QoS |
|---|---|---|---|
| Trigger subscription (IoT Core → device) | `mqtt_subscribe` | `dda/verify/json-trigger/request` | 1 |
| Result publication (device → IoT Core) | `mqtt_publish` | `dda/verify/json-trigger/result` | 1 |

## Expected Trigger_Payload schema

The triggering MQTT message body is a JSON object:

```json
{
  "image_uri":      "optional string — local file path, s3://bucket/key, or http(s):// URL",
  "image_b64":      "optional string — base64-encoded image bytes (PNG/JPEG)",
  "correlation_id": "string — echoed into the Output_Message as 'correlation_id'",
  "station":        { "line": "string — echoed into the Output_Message as 'line' (dotted-path demo)" }
}
```

Rules:

- At least one of `image_uri` / `image_b64` must be present; if **both** are
  present, `image_b64` wins and the URI is never fetched.
- `correlation_id` and `station.line` are the Correlation_Metadata fields the
  workflow's `metadata` node resolves (mappings `correlation_id →
  correlation_id`, `station.line → line`). A missing field is omitted from the
  result (logged, run not failed); a JSON `null` is attached as `null`.
- AWS IoT Core caps MQTT payloads at 128 KB — keep the base64 test image
  small (a compressed PNG of a few KB is plenty).

Example payloads used by the procedure below:

```json
{ "image_uri": "/aws_dda/verify/sample.png",
  "correlation_id": "verify-uri-001",
  "station": { "line": "line-A" } }
```

```json
{ "image_b64": "<output of: base64 -w0 sample.png>",
  "correlation_id": "verify-b64-001",
  "station": { "line": "line-A" } }
```

## Prerequisites

1. **A deployed, working LocalServer component** on the target device
   (`aws.edgeml.dda.LocalServer.<target>`), backend container healthy
   (`GET http://127.0.0.1:5000/health` on the device returns healthy).

2. **`aws.greengrass.ipc.mqttproxy` accessControl covering both topics.**
   The stock LocalServer recipe authorizes
   `aws.greengrass#SubscribeToIoTCore` **only on shadow topics**
   (`$aws/things/*/shadow/name/*`) and `aws.greengrass#PublishToIoTCore` on
   `*`. The trigger subscription to `dda/verify/json-trigger/request` is
   therefore **not covered by default** and must be authorized before the
   trigger can subscribe. Merge the following into the LocalServer
   component's configuration in the Greengrass deployment
   (configurationUpdate → merge → `accessControl`), substituting the
   component name for your target (`arm64JP5` / `arm64JP6` / `arm64JP7` /
   `amd64`):

   ```json
   {
     "accessControl": {
       "aws.greengrass.ipc.mqttproxy": {
         "aws.edgeml.dda.LocalServer.arm64JP6:mqttproxy:verify:1": {
           "policyDescription": "json-trigger-metadata-pipeline verification topics",
           "operations": [
             "aws.greengrass#SubscribeToIoTCore",
             "aws.greengrass#PublishToIoTCore"
           ],
           "resources": [
             "dda/verify/json-trigger/request",
             "dda/verify/json-trigger/result"
           ]
         }
       }
     }
   }
   ```

   Wildcard resources that cover these topics are also acceptable. Result
   publication is already covered by the stock wildcard publish-only policy
   (`mqttproxy:2`), but listing both topics keeps the verification policy
   self-contained and removable.

3. **A vision model already deployed on the target device.** Before importing
   `workflow.json`, replace the `model_inference` node's placeholder:
   open `runbook/workflow.json` and substitute
   `REPLACE_WITH_DEPLOYED_MODEL_NAME` with the name of a model component
   deployed on the device (the model name shown for the device's deployed
   model component in the portal Models page — the same name used when the
   model was packaged). This is the only edit; everything else imports
   unmodified.

4. **Access to publish and observe MQTT messages** against the same AWS IoT
   Core endpoint the device is connected to: the AWS IoT console **MQTT test
   client** (easiest — it can subscribe and publish), or the AWS CLI
   (`aws iot-data publish`) plus the console test client for observing.

5. **Portal access with workflow-editing rights** for the device's use case
   (workflow create → validate → package → deploy).

6. **Device clock in sync** (NTP), so trigger timestamps and log correlation
   are meaningful.

7. **A test image staged on the device for the URI path** (procedure step 2
   stages it; `/aws_dda` is bind-mounted into the backend container, so a
   path under `/aws_dda` is visible to the handler).

## Verification procedure

Run the steps in order. Every step states its observable pass/fail outcome.
Stop at the first failing step and go to Diagnostics.

Shell variables used on the device:

```bash
BACKEND=$(docker ps --filter ancestor=flask-app --format '{{.Names}}' | head -n1)
```

**Step 1 — Record the pre-test restart count.**
On the device:

```bash
docker inspect --format '{{.RestartCount}}' "$BACKEND"
```

Write the number down (call it `R_pre`), together with the current time.

- **Pass:** the command prints an integer; `docker ps` shows the backend
  container `Up ... (healthy)`.
- **Fail:** no container matches `ancestor=flask-app`, or the container is
  not running/healthy — fix the deployment before proceeding.

**Step 2 — Stage the URI-path test image on the device.**

```bash
sudo mkdir -p /aws_dda/verify
sudo cp sample.png /aws_dda/verify/sample.png   # any small PNG/JPEG
```

- **Pass:** `docker exec "$BACKEND" python3 -c "import cv2; assert cv2.imread('/aws_dda/verify/sample.png') is not None"`
  exits 0 (the image is readable and decodable inside the container).
- **Fail:** the file is missing inside the container or does not decode.

**Step 3 — Import the workflow through the backend workflow import
mechanism.**
In the portal Workflow Manager, create a new workflow for the device's use
case from `runbook/workflow.json` (model name already substituted per
prerequisite 3): the definition document is accepted by the workflow create
API (`POST /workflows` with the definition) without modification, then run
validation on it.

- **Pass:** the workflow saves without parse errors and validation reports
  zero errors.
- **Fail:** the definition is rejected at parse or validation — record the
  finding; do not continue.

**Step 4 — Package and deploy the workflow to the device.**
Package the validated workflow version for the device's architecture
(`POST /workflows/{id}/package`), then create a Greengrass deployment to the
device that includes the new Workflow_Component, the LocalServer component
(with the mqttproxy accessControl merge from prerequisite 2), and the model
component. Wait for the deployment to complete.

- **Pass:** the Greengrass deployment reports COMPLETED, and on the device
  `GET http://127.0.0.1:5000/workflows/registrations` lists the workflow with
  `"status": "registered"`.
- **Fail:** the deployment errors/rolls back, or the registration is missing
  or `"invalid"` (the payload's `invalidReason` says why).

**Step 5 — Confirm the trigger is subscribed.**
On the device, fetch the registration detail
(`GET http://127.0.0.1:5000/workflows/registrations/{registration_id}`) and
inspect the `triggerHealth` field.

- **Pass:** the `mqtt_subscribe` node's trigger health `state` is
  `"subscribed"`.
- **Fail:** state is `"connecting"`, `"reconnecting"`, or `"failed"` — the
  `lastError` text and the Diagnostics table below identify the cause (a
  mqttproxy authorization denial appears here).

**Step 6 — Subscribe to the result topic.**
In the AWS IoT console MQTT test client, subscribe to
`dda/verify/json-trigger/result`.

- **Pass:** the console shows the subscription active.
- **Fail:** the console rejects the subscription (IAM/console issue —
  unrelated to the device; fix before continuing).

**Step 7 — Publish the URI-path Trigger_Payload and observe the
Output_Message within 60 seconds.**
Publish to `dda/verify/json-trigger/request` (QoS 1) from the MQTT test
client, or:

```bash
aws iot-data publish --topic dda/verify/json-trigger/request --qos 1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"image_uri":"/aws_dda/verify/sample.png","correlation_id":"verify-uri-001","station":{"line":"line-A"}}'
```

Start a 60-second timer at publish.

- **Pass:** a message arrives on `dda/verify/json-trigger/result` within
  60 seconds of the publish.
- **Fail:** no message within 60 seconds — go to Diagnostics.

**Step 8 — Confirm the URI-path Output_Message content.**
Inspect the received result JSON.

- **Pass:** the payload contains the model's inference results (e.g. the
  model's anomaly/confidence fields) **and** `"correlation_id":
  "verify-uri-001"` **and** `"line": "line-A"`.
- **Fail:** inference results are missing, or either correlation value is
  missing or does not equal the published value.

**Step 9 — Publish the base64-path Trigger_Payload and observe the
Output_Message within 60 seconds.**
Build the payload (on any machine):

```bash
B64=$(base64 -w0 sample.png)
printf '{"image_b64":"%s","correlation_id":"verify-b64-001","station":{"line":"line-A"}}' "$B64" > payload-b64.json
aws iot-data publish --topic dda/verify/json-trigger/request --qos 1 \
  --cli-binary-format raw-in-base64-out --payload file://payload-b64.json
```

Start a 60-second timer at publish.

- **Pass:** a message arrives on `dda/verify/json-trigger/result` within
  60 seconds of the publish.
- **Fail:** no message within 60 seconds — go to Diagnostics.

**Step 10 — Confirm the base64-path Output_Message content.**

- **Pass:** the payload contains inference results **and**
  `"correlation_id": "verify-b64-001"` **and** `"line": "line-A"`.
- **Fail:** inference results are missing, or either correlation value is
  missing or does not equal the published value.

**Step 11 — Backend health check, immediately after the last execution.**
On the device:

```bash
docker ps --filter ancestor=flask-app          # container state
docker inspect --format '{{.RestartCount}}' "$BACKEND"
```

- **Pass:** the container is `Up ... (healthy)` and the restart count equals
  `R_pre` from step 1.
- **Fail:** the container is restarting/exited, or the restart count
  increased — the backend crashed during the test; capture
  `docker logs --tail 500 "$BACKEND"` before anything restarts it.

**Step 12 — Sustained health observation (≥ 10 minutes).**
Wait at least 10 minutes after the last execution (step 9's publish), then:

```bash
docker ps --filter ancestor=flask-app
docker inspect --format '{{.RestartCount}}' "$BACKEND"
docker logs --since 15m "$BACKEND" 2>&1 | grep -iE 'traceback|fatal|abort|sigsegv' || echo CLEAN
```

- **Pass:** the container is still `Up ... (healthy)`, the restart count
  still equals `R_pre` (no crash, no crash-loop), and the log scan prints
  `CLEAN` (or only benign matches you can explain).
- **Fail:** any restart-count increase, a non-running container, or
  crash/abort evidence in the logs within the observation window.

The verification passes when steps 1–12 all pass. Record the device
(target/arch, JetPack version), the model name used, and the two
correlation IDs observed in the commit/PR.

## Diagnostics: no Output_Message within 60 seconds

Work the stages in order; each row names the observation point that tells you
whether that stage completed, so the first failing row localizes the fault.

| # | Stage | Observation point | Healthy signal | If unhealthy |
|---|---|---|---|---|
| 1 | Trigger subscription | `GET /workflows/registrations/{id}` → `triggerHealth` on the device (port 5000); Greengrass log `/greengrass/v2/logs/greengrass.log` | `state: "subscribed"` for the `mqtt_subscribe` node | `failed` with an authorization message ⇒ the mqttproxy accessControl (prerequisite 2) is missing or does not cover `dda/verify/json-trigger/request` — Greengrass log shows the `aws.greengrass.ipc.mqttproxy` "not authorized" denial. `reconnecting` ⇒ Nucleus/IoT connectivity; check the Nucleus log. |
| 2 | Execution start | `GET /workflows/registrations/{id}` → `executions` list (a new `workflow_executions` row appears per firing, with `trigger_context_json` persisted) | A new execution row created at your publish time | No row while the trigger is `subscribed` ⇒ the message never matched: check the exact topic string (`dda/verify/json-trigger/request`), that you published to the same AWS IoT endpoint/region the device uses, and the publisher's own IoT authorization. |
| 3 | Image extraction | `GET /workflows/executions/{execution_id}` → `status`/`error`, and `/workflows/executions/{execution_id}/log` | Execution proceeds past the source node; no image-acquisition error | `status: "failed"` with an error naming `image_b64`, the URI, or "no image source" ⇒ the payload's image field is wrong: bad base64, unreadable/undecodable file at `image_uri` inside the container, or both fields absent. Fix the payload (or restage the image, step 2) and re-publish. |
| 4 | Inference | Same execution `status`/`error` and node status (`/workflows/executions/{execution_id}/node-status`) | The `model_inference` node completes | An error naming the model node ⇒ the substituted model name does not match a model deployed and loaded on the device — re-check prerequisite 3, the model component's deployment state, and the backend log for model-load errors. |
| 5 | Output publication | Execution `status: "completed"` but nothing on the result topic; Greengrass log for `aws.greengrass.ipc.mqttproxy` publish denials | Result message visible in the MQTT test client | A publish authorization denial in the Greengrass log ⇒ `aws.greengrass#PublishToIoTCore` does not cover `dda/verify/json-trigger/result` (prerequisite 2). Otherwise confirm the test-client subscription topic string matches exactly and points at the same IoT endpoint. |

If all five stages look healthy but the message still did not arrive within
60 seconds, capture the execution timing (`GET /workflows/executions/{id}`)
— a cold model load can push the first execution past the bound; re-publish
once warm and re-time.

## Cleanup

After a passing run, remove the verification workflow from the device with a
deployment that excludes the Workflow_Component (keep the LocalServer and
model components in the deployment), and delete `/aws_dda/verify/sample.png`
if staged. The mqttproxy verification policy can be removed in the same
deployment.
