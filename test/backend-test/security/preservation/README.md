# Preservation baseline tests — `security-injection-deserialization-fixes`

These tests implement **Property 2: Preservation — `F(X) = F'(X)` for every
legitimate (non-bug-condition) input** of the
`security-injection-deserialization-fixes` bugfix spec (`bugfix.md` Req 3.1–3.7,
`design.md` "Preservation Checking").

Methodology: **observation-first**. They capture the baseline behavior of the
eight in-scope sites on the **UNFIXED** tree and assert the fixed tree must
match. They are written to **PASS on the unfixed tree** (task 2) and are re-run
**unchanged** after the fix (task 13) to confirm no legitimate behavior changed.

## How to run

From the repo root (bare checkout; no backend image required):

```
python3 -m pytest test/backend-test/security/preservation \
    -p no:cacheprovider --noconftest -v
```

`--noconftest` skips the heavy `test/backend-test/conftest.py` (which needs the
full fastapi/triton stack); these tests load only single source files in
isolation. Hypothesis is already vendored under `.hypothesis/`.

## How baselines are stored / keyed (so task 13 can re-run them)

The baselines are **self-contained in the test files** — each is either a golden
constant / reference model keyed by input, or a checked-in artifact:

| Baseline | Where it lives | Keyed by |
| --- | --- | --- |
| #1 snapshot path + return | `_expected()` reference in `test_preservation_snapshotter.py` (timestamp pinned to `2024-01-02-03-04-05`) | `stationName` |
| #2 SSM command strings | `reference_download()` / `reference_run_mqtt()` templates in `test_preservation_deploy_ssm.py` | the arg tuple |
| #3 `argv` + `(success, output)` | recorded operand vectors + `STUB_RETURN` in `test_preservation_run_command_callers.py` | the call signature |
| #4 tegra/generic decisions | recorded decisions + the passing host_scripts suite in `test_preservation_docker_profile.py` | `(is_gpu, arch)` |
| #5/#6/#7 round-trip structures | `pickle`/`dill` round-trip vs the safe-format round-trip in `test_preservation_deserialization_roundtrip.py` | the payload structure |
| #8 inspect metadata + package | golden asserts in `test_preservation_model_converter.py` | the `.pt` kind / package params |
| #3.7 out-of-scope bytes | `cdk_out_baseline.json` (sha256 of the 11 generated `model_converter.py` copies) + credential-line presence in `test_preservation_out_of_scope_guard.py` | file path |

Task 13 re-runs the **exact same files** against the fixed tree. Because the
pure-validation / model-converter sites are loaded from real source
(`_preservation_support.load_module_from_path`), those tests re-exercise the
fixed code directly; the deserialization tests assert the design's safe-format
round-trip stays structure-equivalent to the current `pickle`/`dill` round-trip.

## Recorded baseline values (UNFIXED tree)

- **#1 Snapshotter (Req 3.1):** valid `^[a-zA-Z0-9_-]+$` name → argv
  `["sh", "/snapshot/snapshot.sh", "/aws_dda/system/snapshot-<name>-<ts>.tar"]`
  and return `"snapshotfile/snapshot-<name>-<ts>.tar.gz"`.
- **#2 deploy.py (Req 3.2):** canonical args (`aarch64` / `22.04` / `3.11` /
  `us-west-2` / `20230918` / 72h / 50KB) produce the recorded download + mqtt
  `AWS-RunShellScript` command lists; `shlex.quote` is a no-op on these clean
  tokens.
- **#3 run_command callers (Req 3.3):** exact operand vectors for
  `create_user` / `delete_user` / `create_group` / `delete_group` /
  `add_user_to_group` / `remove_user_from_group` and `chmod` / `chown` / `chgrp`
  (with and without `-R`); the assertion is invariant to a `--` end-of-options
  sentinel (semantically identical for valid operands). `(success, output)` is
  passed straight through from `run_command`.
- **#4 docker profile (Req 3.4):** `gpu+aarch64 → tegra`, otherwise `generic`;
  the L4T / Orin regression guards pass; the existing 6-test suite passes.
- **#5/#6/#7 deserialization (Req 3.5):** the reference-image map yields the
  `np.vstack` gallery + ordered paths; the camera frame yields the identical
  `{'data','height','width'}` dict (and `None` on timeout/failure); the DIO
  health message yields the identical `{'status','error_type','last_updated'}`
  dict. The design's safe format round-trips to the same structures.
- **#8 model_converter (Req 3.6):** raw state dict / checkpoint inspect to the
  recorded layers / `input_channels` / `num_classes` / `suggested_type`; JIT and
  full-model stay within the detected-or-degrade contract; a legitimate
  classification model produces the recorded manifest / mochi / config package.
  NOTE: this environment runs torch ≥ 2.6 (where `torch.load` already defaults to
  `weights_only=True`), so JIT/full-model currently take the documented
  "Could not inspect model" degrade path; the fix's trusted-source fallback is
  what restores detection on the deployed (older-torch) target.
- **#3.7 out of scope (Req 3.7):** the 11 generated `cdk.out/asset.*`
  `model_converter.py` copies (sha256 in `cdk_out_baseline.json`) and the
  embedded-credential handling in `deploy.py` are unchanged.
