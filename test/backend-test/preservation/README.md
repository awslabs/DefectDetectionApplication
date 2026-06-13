# Preservation baseline tests — `python-3-11-security-upgrade`

These tests implement **Property 2: Preservation — No functional regression for
non-3.9 artifacts** of the `python-3-11-security-upgrade` bugfix spec
(`bugfix.md` Req 3.1–3.8, `design.md` "Preservation Checking").

Methodology: **observation-first**. They capture the externally-observable
baseline behavior on the UNFIXED (Python 3.9) tree and assert the fixed (3.11)
tree must match. They are written to **PASS on the unfixed tree** (task 2) and
are re-run unchanged after the fix (task 11) to confirm no regression.

## What runs where

The backend's full dependency stack (fastapi / pydantic / triton / tinydb /
marshmallow) is only present inside the `flask-app` docker image. Pure-logic
tests run in a bare checkout.

### Runnable now (bare checkout) — executed in task 2

Run from the repo root:

```
PYTHONPATH=src/backend:test/backend-test/utils/streaming \
    python3 -m pytest test/backend-test/preservation \
    -p no:cacheprovider --noconftest -v
```

| File | Req | Approach |
| --- | --- | --- |
| `test_preservation_stream_session.py` | 3.5 | Property-based (stateful) — subscribe / heartbeat / staleness / multi-viewer invariants on the real `StreamBroadcaster` + mock backend |
| `test_preservation_tinydb_roundtrip.py` | 3.8 | Property-based — records in the 3.9 TinyDB on-disk layout load back unchanged via the real `OldTinyDB` JSON read path |
| `test_preservation_distro_python.py` | 3.1, 3.6 | Static — the `g-ir-scanner` system-python shebang and the host model-conversion `python3` (system) usage are UNCHANGED by the fix |

### Deferred to the in-image / runtime gate — tasks 11, 12, 13

| File | Req | Why deferred |
| --- | --- | --- |
| `test_preservation_fastapi_endpoints.py` | 3.4 | Needs `fastapi` / `pydantic` — only present in the `flask-app` image (`importorskip`, skips in a bare checkout) |
| `test_preservation_deferred_runtime.py` | 3.2, 3.3, 3.7 | GStreamer pipeline output, Triton inference, and per-target packaged artifacts require the built image / hardware (integration smoke tests, tasks 12/13) |

## Recorded baseline values (UNFIXED 3.9 tree)

- **`g-ir-scanner` shebang (Req 3.1, must be preserved — distro python, NOT the DDA python):**
  - JP5 (`src/backend/Dockerfile.jp5`): `#!/usr/bin/python3.8` (hardcoded system python)
  - JP6 (`src/backend/Dockerfile.jp6`): dynamically detected `$SYS_PY` (system python),
    fallback `/usr/bin/python3.10`, excludes the DDA interpreter version
- **Host model-conversion (Req 3.6, must be preserved — system `python3`, NOT versioned):**
  `python3 /aws_dda/model_convertor.py` / `convert_model_cleanup.py` and
  `ensure_host_py_deps python3` run on the bare system `python3`.
