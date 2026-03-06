# Multi-Camera HMI Sample

A standalone multi-camera Human-Machine Interface for the DDA (Defect Detection Application). Displays 2 or 4 live camera views with workflow selection and inference execution.

Zero dependencies. No build step. Single HTML file.

## Features

- Connection screen to enter edge device host/IP and port
- Remembers last connected device (localStorage)
- Toggle between 2-camera and 4-camera grid layouts
- Live camera preview polling from image sources
- Assign any image source to any camera slot
- Select a workflow and run inference across cameras
- Per-camera inference with result overlay (Normal/Anomaly)
- Confidence scores, processing time, and anomaly class display
- Works cross-device (CORS already enabled on DDA backend)

## Prerequisites

- Defect Detection Application LocalServer running on the edge device (port 5000 or 5443)
- Any static file server, or just open the file directly

## Quick Start

Serve from any machine on the network:

```bash
cd samples/multi-camera-hmi
python3 -m http.server 3001
```

Open http://localhost:3001, enter the edge device IP (e.g. `192.168.1.100`), and click Connect.

You can also just open `index.html` directly in a browser — no server needed.

## CORS

The DDA backend already has CORS configured with `allow_origins=["*"]`, so cross-origin requests from any host work out of the box.

## API Endpoints Used

| Endpoint | Method | Description |
|---|---|---|
| `/system-health` | GET | Connection test |
| `/image-sources` | GET | List available image sources |
| `/workflows` | GET | List configured workflows |
| `/image-sources/{id}/preview` | POST | Get camera preview frame |
| `/workflows/{id}/run` | POST | Run inference on a workflow |
