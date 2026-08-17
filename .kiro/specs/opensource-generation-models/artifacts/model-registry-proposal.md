# Model Registry Proposal — Dynamic, Admin-Managed Generation Model Catalog

**Task 8.1 deliverable** (_Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_).
Design proposal only — **no code, frontend, or CDK is changed by this exploration**
(Req 9.2). Everything below is a recommendation for a future implementation spec.

Current state it replaces: `MODEL_CATALOG`, a module-level Python list in
`edge-cv-portal/backend/functions/synthetic_core.py` (read 2026-08-17), filtered
at request time by `filter_available_models(catalog, available_model_ids)` where
the available ids come from `bedrock:ListFoundationModels` (IMAGE output
modality). Adding a model today means editing Python and redeploying a Lambda.

## 1. Field coverage — Property 8 (Req 5.2)

Verbatim `MODEL_CATALOG` shape in `synthetic_core.py` (2 entries: Nova Canvas and
Titan Image Generator v2):

```python
{
    "model_id": "amazon.nova-canvas-v1:0",
    "display_name": "Amazon Nova Canvas",
    "capabilities": {"text_to_image": True, "inpainting": True,
                     "image_variation": True, "seed": True, "cfg_scale": True},
    "max_images_per_call": 1,
    "randomization_defaults": {"seed": None, "cfg_scale": 6.5},
}
```

**Coverage table (Property 8: every existing field has a registry attribute).**

| `MODEL_CATALOG` field | Type today | Registry attribute | Registry type | Notes |
|---|---|---|---|---|
| `model_id` | str | `model_id` (**partition key**) | S | unchanged semantics; for Bedrock inference profiles this is the profile id (e.g. `us.stability.stable-image-inpaint-v1:0`) |
| `display_name` | str | `display_name` | S | editable in the admin UI |
| `capabilities.text_to_image` | bool | `capabilities.text_to_image` | BOOL | same key |
| `capabilities.inpainting` | bool | `capabilities.inpainting` | BOOL | same key |
| `capabilities.image_variation` | bool | `capabilities.image_variation` | BOOL | same key |
| `capabilities.seed` | bool | `capabilities.seed` | BOOL | same key |
| `capabilities.cfg_scale` | bool | `capabilities.cfg_scale` | BOOL | same key |
| `max_images_per_call` | int | `max_images_per_call` | N | validated ≥ 1 |
| `randomization_defaults.seed` | int/None | `randomization_defaults.seed` | N / NULL | `None` → DynamoDB NULL; the reader must map NULL back to `None` so `derive_task_seed` behaviour is unchanged |
| `randomization_defaults.cfg_scale` | float | `randomization_defaults.cfg_scale` | N | stored as Decimal; convert to float on read (existing portal DynamoDB convention, `_from_ddb`) |

**Coverage: 10/10 fields expressed. No field of any existing catalog entry is
dropped or renamed.** The registry item is a strict superset — a registry entry
projected onto the ten attributes above is byte-equivalent to a `MODEL_CATALOG`
entry, which is what makes the fallback in §5 safe.

New attributes (the superset part):

| Attribute | Type | Values | Purpose |
|---|---|---|---|
| `provider_type` | S | `bedrock` \| `selfhosted` | selects the Request_Adapter (`integration-proposal.md` §2) |
| `endpoint_config` | M | keyed by environment — see §3 | where and how to invoke a self-hosted model |
| `availability_mode` | S | `always-on` \| `on-demand` | per environment inside `endpoint_config`; a top-level default is kept for display |
| `enabled` | BOOL | true/false | admin kill switch, independent of reachability |
| `inpainting_path` | S | `native` \| `official-variant` \| `community` \| `unsupported` | carried from the Evaluation_Matrix so the UI can warn about community-grade paths |
| `license_disposition` | S | `cleared` \| `legal-review-required` \| `unsuitable` | carried from the Decision_Record; a `legal-review-required` entry cannot be enabled in `prod` (§7) |
| `max_resolution` | N | e.g. 1024 | measured working resolution per Phase C |
| `notes` | S | free text | admin-visible provenance (e.g. "measured 36.7 s/image, offload mode") |
| `created_at` / `updated_at` / `updated_by` | S | ISO-8601 / user id | audit trail, matching the portal's existing item conventions |
| `schema_version` | N | 1 | forward compatibility |

## 2. Storage design (Req 5.2)

**Table:** `dda-portal-generation-model-registry` (DynamoDB, on-demand billing,
PITR on — consistent with the portal's other registries such as
`dda-portal-camera-registry`).

- **Partition key:** `model_id` (S). No sort key: one item per model, small item
  count (order 10), point reads by id and a full `Scan` for the catalog listing —
  the same access pattern `MODEL_CATALOG` has today (whole list per request).
- **Item count/size:** << 1 MB total, so a single `Scan` per `GET /synthetic/models`
  is cheaper and simpler than a GSI. Cache in Lambda module scope for the
  container's lifetime with a short TTL (§6).
- **No GSI proposed.** `provider_type`-filtered queries are a client-side filter on
  a ≤10-item scan; adding a GSI would be premature.

Why DynamoDB rather than SSM Parameter Store or the existing settings table: the
portal already stores registries in DynamoDB with the same Lambda + `boto3.resource`
convention, item-level updates need conditional writes (optimistic concurrency on
`updated_at`), and the admin UI wants per-item audit attributes. The existing
`dda-portal-settings` table (`setting_key` PK) stays the home for *singleton*
config such as `bedrock_configuration` — a per-model registry is not a singleton.

## 3. Per-environment endpoint configuration (Req 5.3)

One registry entry describes a model in **every** environment. `endpoint_config`
is a map keyed by environment name (`prod`, `dev`; extensible):

```json
{
  "model_id": "flux.1-fill-dev",
  "display_name": "FLUX.1 Fill [dev] (self-hosted)",
  "provider_type": "selfhosted",
  "capabilities": {"text_to_image": false, "inpainting": true,
                   "image_variation": false, "seed": true, "cfg_scale": true},
  "max_images_per_call": 1,
  "randomization_defaults": {"seed": null, "cfg_scale": 30.0},
  "enabled": true,
  "inpainting_path": "official-variant",
  "license_disposition": "legal-review-required",
  "max_resolution": 768,
  "endpoint_config": {
    "prod": {
      "kind": "sagemaker-realtime",
      "endpoint_name": "dda-genmodel-flux1-fill-prod",
      "region": "us-east-1",
      "availability_mode": "always-on",
      "invoke_timeout_seconds": 55,
      "instance_type": "ml.g6e.2xlarge"
    },
    "dev": {
      "kind": "sagemaker-async",
      "endpoint_name": "dda-genmodel-flux1-fill-dev",
      "region": "us-east-1",
      "availability_mode": "on-demand",
      "async_output_s3_uri": "s3://dda-portal-artifacts/genmodel-async/flux1-fill-dev/",
      "expected_cold_start_seconds": 364,
      "instance_type": "ml.g6e.2xlarge"
    }
  }
}
```

- `kind` ∈ `bedrock` | `sagemaker-realtime` | `sagemaker-async` | `https`. It, not
  `provider_type`, selects the *transport*; `provider_type` selects the adapter
  family. A `bedrock` entry carries `endpoint_config` with only `{"kind":
  "bedrock", "region": ...}` per environment, so the shape is uniform.
- `availability_mode` lives **inside** each environment block — that is the
  mechanism by which one entry is always-on in `prod` and scale-to-zero in `dev`
  (the exact split the hosting comparison recommends).
- `expected_cold_start_seconds` is what the UI shows the user before a cold
  request and what the worker uses to pick the async pattern
  (`integration-proposal.md` §6).
- The environment key is resolved from the deployment's existing environment
  identifier (the Lambda's stage/env var), never from client input — a client
  cannot ask for another environment's endpoint.
- Missing environment key ⇒ the model is **not available** in that environment
  (fails closed), reported as `endpoint_not_configured` in the models listing.

## 4. Admin UI operations and API surface (Req 5.4)

Four operations, all PortalAdmin-only (§7):

| Operation | Method + path | Request | Response |
|---|---|---|---|
| List (admin view, includes disabled) | `GET /synthetic/registry/models` | — | `{models: [full item…]}` |
| Add | `POST /synthetic/registry/models` | full item minus audit fields | 201 + created item |
| Edit | `PUT /synthetic/registry/models/{model_id}` | full item (or patch subset) + `if_updated_at` for optimistic concurrency | 200 + updated item |
| Enable / disable | `PATCH /synthetic/registry/models/{model_id}` | `{"enabled": true|false}` | 200 + updated item |
| Delete (optional) | `DELETE /synthetic/registry/models/{model_id}` | — | 204 |

- **Validation on write:** `model_id` non-empty and unique; the five capability
  flags present and boolean; `max_images_per_call` ≥ 1; `randomization_defaults`
  keys ⊆ {`seed`, `cfg_scale`}; `provider_type` and every `endpoint_config[env].kind`
  in the closed vocabularies; `selfhosted` entries must carry an
  `endpoint_name`/URL per configured environment. Rejections name the violated
  condition, matching the portal's existing `ValidationError` convention in
  `synthetic_core.py`.
- **Enable guard:** enabling an entry whose `license_disposition` is
  `legal-review-required` or `unsuitable` is rejected for `prod` environments and
  allowed for `dev` with an explicit acknowledgement flag — this is how Req 7.4
  ("flagged models excluded from the production-recommended set") becomes a
  runtime control rather than a document promise.
- **Audit:** every write emits the existing `log_audit_event` record
  (`action='generation_model_registry_write'`, `resource_type='generation_model'`,
  `resource_id=model_id`), same as other admin mutations.
- **UI shape:** a new "Generation Models" panel on the existing portal Settings
  page (where `bedrock-configuration` already lives): table of entries with
  enabled toggle, capability badges, per-environment endpoint/availability
  columns, licence-disposition chip, and a drawer form for add/edit. Reachability
  status (§6) renders as a live badge, distinct from `enabled`.
- **Route-budget note:** if the implementation cannot add API Gateway routes (the
  constraint `data_accounts.py` documents for `bedrock-configuration`, which rides
  the PortalAdmin-only `/data-accounts/{id}` GET/PUT with a reserved id), the same
  trick applies: carry registry CRUD on a reserved id under an existing
  PortalAdmin route. Preferred design is the clean routes above; the fallback is
  recorded so the implementation is not blocked.

## 5. Migration path from the static catalog (Req 5.5)

Four ordered steps, each independently deployable and reversible:

1. **Deploy the table and seed it** from the current `MODEL_CATALOG` (a CDK custom
   resource or a one-shot admin action). Seeded items: `amazon.nova-canvas-v1:0`
   and `amazon.titan-image-generator-v2:0`, `provider_type: "bedrock"`,
   `endpoint_config: {prod: {kind: bedrock, region: us-east-1}, dev: {…}}`,
   `enabled: true`, all ten legacy fields copied verbatim. Nothing reads the table
   yet — zero behaviour change.
2. **Registry-preferred read behind a flag.** `filter_available_models` gains a
   catalog *source*: read the registry when the settings flag
   `generation_model_registry_enabled` (in the existing `dda-portal-settings`
   table, PortalAdmin-writable like `bedrock_configuration`) is true, else use
   `MODEL_CATALOG`. On any registry read error — throttle, missing table, empty
   scan — **fall back to `MODEL_CATALOG` and log**; the pipeline must never lose
   its model list because a table call failed.
3. **Cut over.** Flip the flag in dev, verify `GET /synthetic/models` returns the
   identical payload it returned from the static catalog (a straightforward
   equality assertion, since the registry entry projected onto the ten fields is
   equal by construction), then flip prod.
4. **Retire the constant.** `MODEL_CATALOG` stays in `synthetic_core.py` as the
   seed data and emergency fallback for at least one release after cutover, then
   becomes seed-only. The Titan entry — retired model, still listed today — is
   deleted from the registry at seed time or disabled immediately after, and the
   Nova Canvas entry is flagged with its `LEGACY` lifecycle and 2026-09-30 end of
   life (`cost-model.md` §8).

**Bedrock entries keep working throughout** (the explicit Req 5.5 requirement):
step 1 does not touch the read path; step 2's fallback preserves it on error;
after step 3 a Bedrock entry is served from the registry with `provider_type:
"bedrock"`, which routes to the **unmodified** Bedrock adapter — the byte-identical
Nova Canvas request invariant is preserved by construction
(`integration-proposal.md` §5).

## 6. Availability filtering generalization (Req 5.6)

Today: `filter_available_models(catalog, available_model_ids)` intersects the
catalog with the ids from `bedrock:ListFoundationModels`. Generalized:

```
available(entry, env):
    if not entry.enabled:                      -> unavailable ("disabled")
    if env not in entry.endpoint_config:       -> unavailable ("endpoint_not_configured")
    if entry.provider_type == "bedrock":
        -> available iff entry.model_id in bedrock_available_ids(env.region)
    else:  # selfhosted
        -> available iff endpoint_health(entry, env) in {"InService", "ok"}
```

- **Bedrock ids** keep the existing `ListFoundationModels` (IMAGE output modality)
  intersection, **plus** `bedrock:ListInferenceProfiles` for `us.`-prefixed
  inference-profile ids — required because the live incumbent inpainting model on
  this account is the inference profile `us.stability.stable-image-inpaint-v1:0`,
  which does not appear in `ListFoundationModels` under that id.
- **Self-hosted health:** `sagemaker:DescribeEndpoint` → `EndpointStatus` for
  `sagemaker-realtime`/`sagemaker-async`; an HTTP `GET /health` (2xx) for `https`.
- **On-demand entries must not be marked unavailable while scaled to zero.** For
  `availability_mode: "on-demand"`, `EndpointStatus: InService` with zero instances
  is **available-with-cold-start**: the listing returns
  `{"available": true, "cold_start_expected": true,
  "expected_cold_start_seconds": 364}` so the UI can warn instead of hiding the
  model. Only `Failed` / `OutOfService` / `Deleting` / unreachable is unavailable.
- **Caching** (the requirement's "keep `GET /synthetic/models` fast"): two layers —
  (a) Lambda module-scope memo keyed by `(model_id, env)` with a 60 s TTL, which
  covers bursts within one warm container; (b) a persisted `health_cache` attribute
  on the registry item (`{status, checked_at}`) so a cold container can serve a
  ≤60 s-old status without any control-plane call, refreshing asynchronously.
  Health checks are control-plane calls (`DescribeEndpoint` is throttled per
  account), so uncached per-request probing is not acceptable at ≥10 entries.
- **Fail-soft:** a health-check error yields `available: false` with
  `reason: "health_check_failed"` and never a 5xx on `GET /synthetic/models`; the
  route stays functional with the remaining models, mirroring the existing
  empty-catalog guidance path.

## 7. Authorization boundary (Req 5.7)

Reads and writes are gated by the **existing** portal RBAC — no new auth mechanism:

- **Reads** (`GET /synthetic/models`) keep the current gate: every synthetic route
  calls `_authorize(...)` → `check_user_access(...)`, where `PortalAdmin` /
  `UseCaseAdmin` satisfy `DataScientist` through the role hierarchy in
  `rbac_utils.py` (`Role.VIEWER` 1 … `Role.PORTAL_ADMIN` 5). Unchanged.
- **Writes** (add / edit / enable / disable / delete) are **PortalAdmin-only**,
  implemented exactly like the existing `bedrock-config:write` boundary: a new
  `Permission.GENERATION_REGISTRY_WRITE` granted only to `Role.PORTAL_ADMIN` in
  `shared_utils.py`'s role→permission map, enforced with the existing
  `super_user_required` / `require_super_user` decorator (or the
  `MANAGE_BEDROCK_CONFIG`-style operation grouping in `rbac_middleware.py`).
  `UseCaseAdmin` deliberately does **not** get it: a registry entry is
  account-global infrastructure (an endpoint costing $1,600–$8,200/month per
  `cost-model.md`), not use-case data.
- **Denials** return 403 and log the existing `unauthorized_access` audit event,
  identical to the current synthetic routes.
- **Endpoint invocation permissions are separate from registry permissions:** the
  worker's execution role gets `sagemaker:InvokeEndpoint` / `InvokeEndpointAsync`
  scoped to the endpoint ARNs the registry may name (an ARN prefix such as
  `arn:aws:sagemaker:us-east-1:164152369890:endpoint/dda-genmodel-*`). An admin
  editing `endpoint_name` therefore cannot redirect generation at an arbitrary
  endpoint outside that prefix — the IAM policy, not the registry, is the trust
  boundary. This constraint should be stated in the implementation spec, because a
  free-text `endpoint_name` in a database is otherwise a privilege-escalation path.
- **`https` entries** must resolve to an allow-listed internal host (VPC-internal
  ALB DNS or a Secrets Manager-stored base URL id, not a raw admin-supplied URL),
  for the same reason: otherwise registry write access becomes arbitrary
  server-side request forgery. Recommendation: `endpoint_config[env]` stores a
  *reference* (`secret_id`), never a raw URL.

## 8. Requirement coverage

| AC | Where satisfied |
|---|---|
| 5.1 proposal exists as a design document | this document |
| 5.2 schema expresses every `MODEL_CATALOG` field + provider type, endpoint config, availability mode, enabled | §1 coverage table (10/10) + §1 superset table + §2 |
| 5.3 per-environment endpoint configuration with distinct Availability_Modes | §3 |
| 5.4 admin UI add / edit / enable / disable with API surface | §4 |
| 5.5 migration path keeping Bedrock entries functional | §5 |
| 5.6 availability filtering generalization (Bedrock + endpoint health, cached) | §6 |
| 5.7 writes restricted to portal administrators via the existing authorizer | §7 |
| Property 8 (registry schema field coverage) | §1, cross-checked against `synthetic_core.py` as read on 2026-08-17 |
