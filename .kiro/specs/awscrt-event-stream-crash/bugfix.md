# Bugfix: LocalServer backend crash-loop via awscrt event-stream abort

## Status (handoff)

Root cause IDENTIFIED and fix APPLIED in the working tree. **Not yet built,
not yet device-verified.** Resume at "Remaining work" below.

## Symptom

On jetson-thor1 (JP7, LocalServer.arm64JP7 1.0.14) the backend container
restarts every ~2-30 minutes, indefinitely. Observed 40 restarts in one
afternoon. Restart cadence from the container's own uvicorn "Started server
process" lines (2026-08-30/31 UTC): 23:36:44, 23:43:46, 23:45:18, 23:56:50,
00:07:53, 00:20:56, 00:22:58, 00:29:30, 00:32:02, 00:42:34, 01:50:43.

Misleading surface signals (these sent the first investigation pass the wrong
way — do not re-chase them):

- `docker inspect` reports `exit=0`, `oom=false`, `health=healthy`. The exit
  code is 0 because the container's PID 1 wrapper exits cleanly AFTER the
  Python process dies; the abort itself is not visible in the exit code.
- The vLLM reconciler's repeated `qwen3-5-9b` failure ("model type `qwen3_5`
  but Transformers does not recognize this architecture") and its "STARVED
  DEVICE ... Recovery requires a BACKEND CONTAINER RESTART" latch are LOUD in
  the logs at every restart but are NOT the cause: that code path only logs,
  it never restarts anything. It is a separate, real, unrelated bug.
- `restart: always` in `src/docker-compose.yaml` is what re-launches the
  container; it is correct and intentional, not the bug.

## Root cause

`awscrt` was pinned to **0.14.7** (a JetPack-4-era pin, forced by
`awsiotsdk==1.11.9`). That version's bundled `aws-c-event-stream` carries the
fatal "Continuation ref count has gone negative" defect: the native library
calls `abort()` inside the event-stream RPC continuation teardown, killing the
whole Python process. The compose file's own `restart: always` comment already
documented this abort as a known hazard — this is that hazard firing in a loop.

Evidence: the last output of every backend life is a native crash dump —
`libc abort()` → `__gnu_cxx::__verbose_terminate_handler` → frames inside
`/usr/local/lib/python3.11/dist-packages/_awscrt.cpython-311-aarch64-linux-gnu.so`,
with `swig/python detected a memory leak of type 'Guid *'` and a
`Stack trace:` banner immediately before. Confirmed in-container versions:
`awscrt 0.14.7`, `awsiotsdk 1.11.9`.

Why this device hits it constantly: each backend life opens several Greengrass
IPC event-stream connections — three workflow trigger subscriptions
(`quality/invoke`, `quality/invoke-dgbi`) plus `dda-camera-registry` /
`dda-camera-bindings` / `dda-model-status` shadow traffic. More continuations
churning = more chances to hit the refcount defect. A backend audit found NO
self-termination path in DDA code (`app.py`'s `os._exit` runs only after
uvicorn's server has already returned; every other `sys.exit` is in a separate
process — `healthcheck.py`, `python_bridge.py` handler subprocesses,
`dlr_disable_phone_home.py`, two CLI mains).

Ruled out: nvargus/CSI poisoning (`csi-nvargus-optional` spec). That defect
blocks CUDA context creation device-wide; it does not abort the process, and
the crash dumps carry no CUDA/nvargus frames. Unrelated to this loop.

## Fix applied (in the working tree)

`awsiotsdk` **1.11.9 → 1.31.0**, which pins `awscrt` **0.14.7 → 0.36.1**.

- `src/backend/requirements.txt`: the awsiotsdk pin.
- All five backend Dockerfiles (`Dockerfile`, `.jp5`, `.jp6`, `.jp7`,
  `.x86_64_nvidia`): replaced the awscrt 0.14.7 **source build** — including
  the `libcrypto.a` move-aside hack that dodged the vendored aws-lc / system
  OpenSSL link collision (`undefined symbol: EVP_aead_aes_128_gcm_tls13`) —
  with a plain wheel install:
  `pip install --only-binary :all: awscrt==0.36.1`, verified by
  `import awscrt.mqtt, awscrt.eventstream`. Modern awscrt publishes
  `cp311-abi3-manylinux2014_aarch64` wheels, so no source build and no OpenSSL
  trap. `--only-binary` makes a silent fallback to a source build impossible.

API compatibility: DDA uses only the stable `awsiot.greengrasscoreipc` V1
surface (client/model imports across ~15 modules, verified by grep). No
`mqtt_connection_builder` or other churn-prone API is used.

Baselines rebaselined in the same change (builds.md rule), all suites green:

- `test/backend-test/security/baselines/dependency_baseline_requirements.txt`
- `.../docker_baseline_out_of_scope.json` (src/backend/Dockerfile sha256)
- `.../docker_baseline_backend_Dockerfile.jp{5,6,7}_masked.txt`
- `test/backend-test/backend_jammy_pkgs/baselines/backend_Dockerfile.jp{5,6,7}.sha256.txt`,
  `backend_Dockerfile.x86_64_nvidia.sha256.txt`,
  `backend_Dockerfile_libssl_masked.txt`

Verification so far (host):
- `test/backend-test/security/preservation`: 138 passed, 8 skipped
- `test/backend-test/backend_jammy_pkgs`: 46 passed
- both together: 184 passed, 8 skipped
- `awsiotsdk==1.31.0` installed in the host test venv; `import awsiot` OK
  (awscrt 0.36.1); `test/backend-test/utils/test_ipc_client.py`: 5 passed

## Remaining work

1. Run the broader backend suite against awsiotsdk 1.31.0 for import/API
   regressions (mqtt, shadow, gg_utils, trigger_runtime, camera_sync,
   user_accounts_sync — everything in the grep list above).
2. Build `aws.edgeml.dda.LocalServer.arm64JP7` (builds.md: one build at a
   time; preservation gate is already green; move `cdk.out` aside). Watch for
   the new `awscrt wheel OK` line and confirm no source build happens.
3. Deploy to jetson-thor1 and verify the crash loop is GONE: container
   `RestartCount` stable over a sustained window (hours, not minutes) with
   workflow triggers and shadow traffic active. The pre-fix baseline was 40
   restarts/afternoon, so a few hours flat is a decisive result.
4. Consider JP5/JP6 builds too — the pin change affects every arch, so each
   arch needs its own on-device check per the steering rule.
5. Separately: the `qwen3-5-9b` transformers-architecture failure and its
   memory-starvation latch are a real unrelated bug worth their own spec.

## Notes for the next session

- Device access: AWS IoT secure tunneling. `.dda_tunnel_proxy.py` is a
  source-mode local proxy; tokens are SINGLE-USE, so a bare retry loop fails
  with "The access token was previously used". `.dda_tunnel_keepalive.sh`
  rotates the source token via `rotate_tunnel_access_token` before every
  reconnect — use that. SSH: `sshpass -p lookout ssh -p <port> aws@localhost`,
  sudo password `lookout`.
- The `aws` CLI's `ssm` subcommand is broken on the dev host ("badly formed
  help string"); use boto3 for SSM.
