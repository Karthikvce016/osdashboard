# OS Performance Dashboard

Real-time, cross-platform OS performance monitoring dashboard.

## Quick Start

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Start the server
python -m backend.main
```

Open **http://localhost:8000** in your browser.

## Stack

| Layer    | Technology                        |
|----------|-----------------------------------|
| Backend  | Python, FastAPI, psutil, WebSockets |
| Frontend | HTML, CSS, JavaScript, Chart.js   |

## Features

- Real-time CPU & memory charts (60-sample history)
- Per-core usage bars
- Sortable, filterable process table
- Process termination (Kill button)
- Configurable update interval (1–10 s)
- Pause / Resume graph updates
- Anomaly highlighting (green → yellow → red)
- System health indicator (Normal / High Load / Critical)
