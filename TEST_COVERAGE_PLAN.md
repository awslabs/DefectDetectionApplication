# Backend Test Coverage Plan (prioritized)

Status: P0 COMPLETE (2026-06-01). P1/P2 remain. Tracked in the branch
(web-portal-development-rebased) so the next session can pick it up.

## Progress log
- 2026-06-01: P0 #1, #2, #3 all done, merged on web-portal-development-rebased,
  and verified green (46 passed) running inside the flask-app image on the build
  box during a full `./gdk-component-build-and-publish.sh aarch64 5` run.
  - utils/auth.py            -> test/backend-test/utils/test_auth.py
  - endpoints/auth_info.py   -> test/backend-test/api-endpoints/test_auth_info_api.py
  - user/group + dda-user    -> test/backend-test/utils/test_user_group_management_utils.py
                                test/backend-test/utils/test_dda_user_management_utils.py
  - In-build test gate added to build-custom.sh (runs the four P0 suites inside
    the freshly built flask-app image; fails the build on test failure;
    SKIP_BACKEND_TESTS=1 to bypass).

## How to run the backend tests
The tests import the full backend (edgemlsdk native bindings, gstreamer,
libtritonserver.so), so they only run where those deps exist — i.e. inside the
built flask-app image, NOT in a bare checkout.

Two ways:
1. Automatically, as part of the build: build-custom.sh runs the P0 suites after
   the images build. Watch for "Backend unit tests passed." Set
   SKIP_BACKEND_TESTS=1 to skip (e.g. offline box; the step pip-installs test deps).
2. Manually against an existing flask-app image:
```bash
docker run --rm -v "$PWD":/repo -w /repo --entrypoint bash flask-app -c '
  python3.11 -m pip install --no-cache-dir --quiet pytest pytest-cov sarge testfixtures
  export PYTHONPATH=/repo/src/backend
  export LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
  python3.11 -m pytest test/backend-test/... -v
'
```

### Hard-won gotchas for the in-image harness (read before adding tests)
- cwd MUST be the repo root (/repo). conftest.py and LocalServerBaseTestCase
  build paths as os.getcwd() + "test/backend-test/utils".
- LD_LIBRARY_PATH MUST include /opt/tritonserver/lib or collection fails with
  "ImportError: libtritonserver.so: cannot open shared object file".
- The runtime image has fastapi 0.109.2 / starlette 0.36.3, where TestClient
  leaves request.client = None. AccessLogRoute.log_info dereferences
  request.client.host, so ANY TestClient-based endpoint test 500s in this image
  (the existing test_station_logo_api.py fails here too). Prefer calling the
  endpoint handler function directly (see test_auth_info_api.py) OR fix
  access_log_router.log_info to be null-safe on request.client first.

## Coverage report
pytest-cov is in the documented deps. Inside the image (per above), add e.g.:
  --cov=utils.auth --cov=endpoints.auth_info --cov-report=term-missing
For the whole suite, point --cov=src/backend and --cov-report=html:coverage_html.

## Already well covered (do NOT reinvest)
- API endpoints: camera, captured_images, feature_configuration, image_source
  (+config), inference_result, input/output_configuration, list_images,
  station_logo, system_health, workflows
- dda_triton/* (convert models, lfv<->triton, autostart, edge client, cleanup)
- resources/accessors/* (workflow, image_source, inference_result, latency, ...)
- utils: camera_manager, captured_images_utils, digital_input (process+thread),
  feature_configs, get_is_triton, inference_results_utils, gg_utils,
  list/stop/restart components
- gstreamer pipeline builder + executor, dao/sqlite_db/db_migration,
  lyra anomalies mask utils
- endpoints/system.py get_local_server_component_version (version resolution)

## Priority gaps (ranked by criticality x current coverage)

### P0 — security-critical (DONE 2026-06-01)
1. [DONE] utils/auth.py (validate_token, validate_remotely) — token-validation
   gate for the whole API. token present/absent with auth enabled vs disabled,
   introspection active:false -> 401, upstream error -> 500, valid token passes.
2. [DONE] endpoints/auth_info.py (/authorization-configurations) — enabled vs
   disabled shapes AND clientSecret never leaks into the response.
3. [DONE] utils/user_group_management_utils.py + utils/dda_user_management_utils.py
   — privileged OS user/group + file-permission logic.

### P1 — core data integrity / runtime stability, no coverage (NEXT)
4. endpoints/download_file.py (231 lines) — file export/download; path-traversal
   surface. Only indirectly touched today. Test path construction + access scoping.
   NOTE: it exposes an `unauthenticated_router` (see app.py) — verify what is
   reachable without auth. Likely needs direct-handler calls (TestClient caveat above).
5. dao/iotshadow/IoTShadowAccessor.py / CloudIoTShadowAccessor.py — device<->cloud
   config source of truth; only used as Mock(spec=...) elsewhere, never exercised.
6. mqtt/PublishHandler.py / SubscriptionHandler.py — no tests. Command/state path.
7. defect_detection_config/defect_detection_config.py (165 lines) — central config
   loader used everywhere; in base setup but never asserted.

### P2 — functional correctness, moderate blast radius
8. triggers/outputs/dio.py (159 lines) — digital-output actuation; managers are
   tested but this output path isn't.
9. utils/capture_task_manager.py — task scheduling/lifecycle; only mocked today.
10. metrics/collector.py + latency_metrics.py — Timer used in auth.py; cheap.
11. endpoints/workflow.py (413 lines) — has API tests; run real coverage to find
    untested branches before adding more.

## Suggested order (remaining)
P1 #4 (download_file — path traversal, highest security value of P1), then #5–#7,
then P2. Consider gating the build on a coverage threshold now that the in-build
test step exists.

## Possible product hardening surfaced while testing (optional, not yet done)
- access_log_router.log_info dereferences request.client.host with no null
  guard. A real request without client info (or TestClient) 500s. A one-line
  defensive fix (`req_body.client.host if req_body.client else "-"`) would also
  unblock TestClient-based endpoint tests in the in-image harness.
