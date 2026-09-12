# Quality Station HMI

A browser-based kiosk app for the quality station's 1920x1080 monitor. It
polls the device's LocalServer REST API and shows, in real time, each
workflow run's inspection verdict (`is_anomalous`, `confidence`,
`generated_text`), the captured frame, and — for workflows with a
reference-comparison node (`llm_inference`/VLM or `bedrock_inference`) — the
configured reference image side by side with the captured frame.

Spec: `.kiro/specs/quality-station-hmi/` (requirements, design, tasks).

## Architecture: no Node backend

Node.js is a **build-time dependency only** (Vite + TypeScript + Vitest).
The build output in `hmi/dist/` is a plain static-asset bundle — no
server-side rendering, no runtime framework, no Node process on the device.

The device's existing LocalServer (the FastAPI backend in `src/backend`)
serves the bundle itself. `src/backend/app.py` mounts it at `/hmi`:

- The mount is **guarded by directory existence**: if the dist directory is
  absent, the backend behaves exactly as before and `/hmi` returns 404.
- The directory defaults to `<repo>/hmi/dist` (resolved relative to
  `app.py`) and can be overridden with the `HMI_DIST_DIR` environment
  variable.
- Serving is same-origin with the API, so there is no CORS setup, no second
  web server, and one TLS certificate. Every data route the HMI calls still
  requires the session token; image routes use token-in-query, designed for
  browser `<img>` loads.

## Prerequisites

- Node.js 18 or newer (Vite 5 requirement) and npm — build machine only
- No dependencies on the device beyond the LocalServer that is already there

## Build

```bash
cd hmi
npm ci          # exact versions from package-lock.json
npm run build   # tsc --noEmit type check, then vite build → dist/
```

The bundle is emitted to `hmi/dist/` with sourcemaps (kept intentionally so
the bundle stays inspectable on the device).

## Test

```bash
cd hmi
npm test        # vitest --run (~150 tests, incl. fast-check properties)
```

All pure logic (run selection, image pairing, verdict formatting, history,
session/startup decisions) is unit- and property-tested; the API client and
auth flow are tested with injected fakes. Tests run in Node — no browser
needed.

The Triple Inspection kiosk layout (`/hmi/triple.html`) additionally has a
Playwright suite, because its requirements are about rendered geometry — slot
widths, image panel aspect, verdict text height, overflow at 1280 and 1920 px
— which jsdom cannot measure:

```bash
cd hmi
npx playwright install chromium   # once per machine
npm run test:layout               # builds dist/, then measures triple.html
```

The suite serves the built bundle and every stubbed LocalServer response
through request interception, so it needs no server, no device, and no
network. It is a `.spec.ts` file, so `npm test` (Vitest) never picks it up.

## Local development

`npm run dev` starts the Vite dev server, but the app calls its API
same-origin, so on the dev server those calls hit Vite and fail. Two
options:

1. **Serve the built bundle through a locally running LocalServer** (closest
   to production): `npm run build`, then start the backend — it picks up
   `hmi/dist` automatically and serves the app at `http://localhost:5000/hmi/`
   (or `https://…:5443/hmi/` when auth is enabled).
2. **Add a Vite dev proxy** in `vite.config.ts` (`server.proxy`) that
   forwards `/workflows` and `/local-auth` to a running device or local
   backend. Not committed by default; add it locally if you want hot reload.

## Deploy to a device

1. Build on your workstation: `cd hmi && npm ci && npm run build`.
2. Get `dist/` onto the device where the backend can see it. Depending on
   how the LocalServer runs on that device:
   - **Backend running from a repo checkout**: copy to `<repo>/hmi/dist` —
     the default path, no configuration needed.
   - **Backend running in the LocalServer container** (`src/docker-compose.yaml`):
     the container image does not include `hmi/`, so either copy `dist/`
     to a host path that is already bind-mounted into the container (for
     example under `/aws_dda`) and set `HMI_DIST_DIR` to that in-container
     path in the compose environment, or add a dedicated bind mount for it.
3. Restart the backend so the mount guard re-evaluates. Devices without the
   bundle are unaffected — the mount simply doesn't register.
4. Verify: `https://<device>:5443/hmi/` (auth enabled) or
   `http://<device>:5000/hmi/` (auth disabled) serves the app's
   `index.html`.

## Deploy detached from the LocalServer (no Docker, own port)

The `/hmi` mount above is optional. Because the bundle is static assets, it
can be served completely independently of the LocalServer and of Docker —
useful when you don't want a kiosk change to require a component rebuild and
redeploy. `serve-hmi.sh` does this with nothing but `python3`:

```bash
# on the device
./serve-hmi.sh                       # port 8081, ./dist, API on this host:5000
./serve-hmi.sh 9000                  # a different port
PORT=9000 BIND=127.0.0.1 ./serve-hmi.sh   # same, via environment
```

Then open `http://<device>:8081/triple.html` — no query string needed.

### Telling the page where the API is

Served detached, the page's own origin (`:8081`) is a static file server, so
`API_Base` has to point at the LocalServer instead. `serve-hmi.sh` stamps it
into the served HTML's `dda-api-base` meta tag:

```bash
API_BASE=5000                        # default: LocalServer on this host:5000
API_BASE=http://192.168.8.224:5000   # LocalServer on another machine
API_BASE= ./serve-hmi.sh             # stamp nothing (same-origin, as before)
```

Stamping happens in a temp copy, so the built `dist/` is never modified.

`api/base.ts` resolves `API_Base` synchronously, first usable value winning:

1. the page URL's `?api=` parameter — `?api=5000`, `?api=http://host:5000`
2. the `dda-api-base` meta tag (what `serve-hmi.sh` stamps)
3. the build-time `VITE_API_BASE`
4. same-origin (empty) — the original behaviour, so a bundle served by the
   LocalServer's `/hmi` mount produces byte-identical URLs

A value that cannot be parsed falls through to the next source rather than
throwing, so a typo can't brick the kiosk. `API_BASE` itself is validated at
startup and refuses to serve on a malformed value.

Cross-origin works because the backend already sends
`Access-Control-Allow-Origin: *`. Note the bundle must be built with relative
asset URLs to be served at a server root:

```bash
npx vite build --base=./ --outDir dist
```

A bundle built with the default `/hmi/` base is detected and served under a
`/hmi/` path prefix instead, so its absolute asset URLs still resolve.

## Kiosk setup

Point the station's browser at the app in kiosk mode — either the
LocalServer mount:

```bash
chromium --kiosk https://<device>:5443/hmi/
```

or the detached server:

```bash
chromium --kiosk http://<device>:8081/triple.html
```

Use port 5000 over plain HTTP when the device runs with local auth
disabled. The layout targets 1920x1080 full screen; it also stays usable at
1280 px wide.

## Login and session behavior

- On first load (or an expired session) the app asks the unauthenticated
  `GET /local-auth/status`. A device reporting local login **disabled** issues
  no tokens and requires none, so the app is entered directly with no form —
  presenting one there is a dead end whose only possible outcome is a 403.
- Otherwise the app shows a login form and submits to `POST /local-auth/login`.
  An unreachable or unparseable status also keeps the form, rather than
  entering an app that would then fail every request.
- On success, the session token and its expiry are stored in
  `localStorage["hmi.session"]`, so a kiosk page reload resumes without
  prompting. Credentials are kept in memory only, never persisted; they back
  a single automatic re-login if a request hits a 401.
- The app polls run state every 2 seconds and refreshes the registration
  list about every 30 seconds to pick up newly deployed workflows.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `/hmi/` returns 404 | `dist/` missing at the resolved path — check `HMI_DIST_DIR` and that the backend was restarted after copying |
| Login form appears on a device with local login **off** | The page could not reach `GET /local-auth/status`. Served detached, that means `API_Base` is unset or wrong, so the probe went to the static server and 404'd — start with `API_BASE=5000` or append `?api=5000` |
| Login form says local login is disabled after submitting | The device's LocalServer runs with local auth turned off (login route returns 403); the app should not have shown the form at all — see the row above |
| Images broken but text data fine | Image routes take the token as a query parameter — usually a stale session; reload to re-login |
| App loads but no workflow shown | No `registered` workflow registrations on the device, or the poller can't reach the API (check the on-screen connection state) |
