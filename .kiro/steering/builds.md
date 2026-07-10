---
inclusion: always
---

# Greengrass Component Builds (JP5 / JP6)

## CRITICAL: never run two component builds at the same time

JP5 and JP6 (and any other target) builds **must run strictly one at a time**.
Running two builds concurrently **corrupts the model versioning** (the builds
share the `NEXT_PATCH` version resolution plus the working directories and
docker image tags — `greengrass-build/`, `custom-build/`, and the shared
`edgemlsdk` / `flask-app` / `react-webapp` image tags — so concurrent runs
clobber each other and produce wrong/duplicate model versions).

**If two builds are ever running at once: STOP BOTH immediately, then restart
one at a time.** Do not let a second build start until the first has fully
finished.

## How to build

`gdk component build` builds the single component named in `gdk-config.json`.
To build multiple targets, build them **sequentially**, swapping the component
name in `gdk-config.json` between runs (JP6 =
`aws.edgeml.dda.LocalServer.arm64JP6`, JP5 =
`aws.edgeml.dda.LocalServer.arm64JP5`). The target (JP5 vs JP6) is derived from
the component name by `build-custom.sh`.

- Build only (no AWS creds needed): `gdk component build`.
- `gdk-config.json` is a build artifact excluded from commits; swapping it per
  target is fine, but restore it when done.
- Each target runs a full GPU `onnxruntime` source build by default
  (`ONNXRUNTIME_GPU=1` for JP5/JP6), so a single target takes ~1–2h.
- Capture each target's output to its own log: `.gdk_build_jp6.log` /
  `.gdk_build_jp5.log`.

## Before dispatching any build

Check that no build is already running:

```
pgrep -af "gdk component build"
pgrep -af "build-custom.sh"
```

If either returns a process, do **not** start another build — wait for it to
finish (or stop it) first.
