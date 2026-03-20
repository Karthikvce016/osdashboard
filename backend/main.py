"""
main.py — FastAPI application.

- Serves the frontend static files
- WebSocket endpoint /ws for real-time metric streaming
- REST endpoint POST /kill/{pid} for process termination (localhost-only + token)
- Background tasks: fast metrics (every interval) and slow metrics (every 10s)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import collector
from .collector import (
    get_cpu_usage,
    get_memory_usage,
    get_per_core_usage,
    get_process_list,
    get_disk_io_rates,
    get_network_io_rates,
    get_load_average,
)
from .history import MetricHistory
from .processor import format_bytes
from .systeminfo import get_system_info

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_CLIENTS = 20
DEFAULT_INTERVAL = 1.0  # seconds
SLOW_METRIC_INTERVAL = 10.0  # seconds — for system info refresh
KILL_TOKEN = os.environ.get("DASHBOARD_KILL_TOKEN", "")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# ---------------------------------------------------------------------------
# State container — replaces module-level globals
# ---------------------------------------------------------------------------


@dataclass
class ClientSession:
    """Per-client WebSocket state."""
    ws: WebSocket
    interval: float = DEFAULT_INTERVAL


@dataclass
class DashboardState:
    """Central mutable state for the dashboard."""
    history: MetricHistory = field(default_factory=MetricHistory)
    clients: dict[int, ClientSession] = field(default_factory=dict)
    _next_id: int = 0

    # Cached slow-metric data
    system_info: dict = field(default_factory=dict)
    last_slow_update: float = 0.0

    # Last values for anomaly detection
    last_cpu: float = 0.0
    last_mem: float = 0.0
    last_proc_count: int = 0

    # Last sampled instantaneous metrics
    last_disk: dict = field(default_factory=dict)
    last_network: dict = field(default_factory=dict)
    last_load: dict = field(default_factory=dict)

    def add_client(self, ws: WebSocket, interval: float = DEFAULT_INTERVAL) -> int:
        """Register a client; returns its session ID."""
        cid = self._next_id
        self._next_id += 1
        self.clients[cid] = ClientSession(ws=ws, interval=interval)
        return cid

    def remove_client(self, cid: int) -> None:
        self.clients.pop(cid, None)

    @property
    def min_interval(self) -> float:
        """Return the fastest requested interval across all clients."""
        if not self.clients:
            return DEFAULT_INTERVAL
        return max(0.5, min(c.interval for c in self.clients.values()))


state = DashboardState()


# ---------------------------------------------------------------------------
# Lifespan — replaces deprecated @app.on_event("startup")
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background metric tasks and clean up on shutdown."""
    task_fast = asyncio.create_task(_fast_metric_loop())
    task_slow = asyncio.create_task(_slow_metric_loop())
    yield
    task_fast.cancel()
    task_slow.cancel()


app = FastAPI(title="OS Performance Dashboard", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def root():
    """Serve the dashboard HTML."""
    index = FRONTEND_DIR / "index.html"
    return HTMLResponse(
        content=index.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Enforce connection limit
    if len(state.clients) >= MAX_CLIENTS:
        await ws.close(code=1013, reason="Server is at maximum capacity")
        return

    await ws.accept()
    cid = state.add_client(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if "interval" in msg:
                    state.clients[cid].interval = max(0.5, min(10.0, float(msg["interval"])))
            except (json.JSONDecodeError, ValueError, KeyError):
                pass
    except WebSocketDisconnect:
        state.remove_client(cid)
    except Exception:
        state.remove_client(cid)


# ---------------------------------------------------------------------------
# Process kill endpoint — localhost-only + optional token
# ---------------------------------------------------------------------------


@app.post("/kill/{pid}")
async def kill_process(pid: int, request: Request):
    """Attempt to terminate a process by PID.

    Security:
    - Only requests from 127.0.0.1 / ::1 are allowed.
    - If DASHBOARD_KILL_TOKEN env var is set, the request must include
      a matching Authorization: Bearer <token> header.
    """
    # Localhost check
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse(
            {"status": "error", "message": "Kill endpoint is restricted to localhost"},
            status_code=403,
        )

    # Token check (if configured)
    if KILL_TOKEN:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], KILL_TOKEN):
            return JSONResponse(
                {"status": "error", "message": "Invalid or missing auth token"},
                status_code=401,
            )

    # Basic safeguards: never allow killing PID 0/1 or the dashboard process itself.
    protected_pids = {0, 1, os.getpid()}

    if pid in protected_pids:
        return JSONResponse(
            {"status": "error", "message": f"PID {pid} is protected and cannot be terminated"},
            status_code=403,
        )

    try:
        proc = psutil.Process(pid)
        name = proc.name()
        if collector.is_risky_process(name):
            return JSONResponse(
                {"status": "error", "message": f"{name} is classified as a critical system process"},
                status_code=403,
            )

        proc.terminate()
        return JSONResponse({"status": "ok", "pid": pid, "name": name})
    except psutil.NoSuchProcess:
        return JSONResponse({"status": "error", "message": f"Process {pid} not found"}, status_code=404)
    except psutil.AccessDenied:
        return JSONResponse({"status": "error", "message": f"Access denied for PID {pid}"}, status_code=403)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_status(cpu: float, mem: float) -> str:
    """Derive overall system health label."""
    if cpu > 90 or mem > 90:
        return "Critical"
    if cpu > 70 or mem > 70:
        return "High Load"
    return "Normal"


def _build_alerts(cpu: float, mem_pct: float, mem_pressure: float, disk: dict, net: dict, load: dict) -> list[dict]:
    """Generate structured alert objects based on current metrics."""
    alerts: list[dict] = []

    if cpu > 85:
        alerts.append({"level": "critical" if cpu > 95 else "warning", "source": "cpu", "message": "High CPU usage"})

    if mem_pressure > 85:
        alerts.append(
            {
                "level": "critical" if mem_pressure > 95 else "warning",
                "source": "memory",
                "message": "Memory pressure is high",
            }
        )

    if disk.get("read_mb_s", 0) > 50 or disk.get("write_mb_s", 0) > 50:
        alerts.append(
            {
                "level": "warning",
                "source": "disk",
                "message": "High disk throughput",
            }
        )

    if net.get("up_mb_s", 0) > 50 or net.get("down_mb_s", 0) > 50:
        alerts.append(
            {
                "level": "warning",
                "source": "network",
                "message": "High network throughput",
            }
        )

    # Load average relative to logical cores
    cores = psutil.cpu_count(logical=True) or 1
    if load.get("one", 0) > cores * 1.5:
        alerts.append(
            {
                "level": "warning",
                "source": "load",
                "message": "1-minute load average is high for core count",
            }
        )

    return alerts


async def _broadcast(payload: dict):
    """Send JSON payload to all connected clients concurrently.

    Slow clients no longer block others thanks to asyncio.gather.
    Stale connections are cleaned up automatically.
    """
    if not state.clients:
        return

    data = json.dumps(payload)

    async def _safe_send(cid: int, session: ClientSession):
        try:
            await session.ws.send_text(data)
        except Exception:
            return cid  # mark as stale
        return None

    results = await asyncio.gather(
        *[_safe_send(cid, s) for cid, s in list(state.clients.items())],
        return_exceptions=True,
    )

    # Remove stale clients
    for result in results:
        if isinstance(result, int):
            state.remove_client(result)


# ---------------------------------------------------------------------------
# Background metric loops — fast + slow split
# ---------------------------------------------------------------------------


async def _fast_metric_loop():
    """Collect fast-changing metrics and broadcast at the min client interval.

    Each collector is wrapped in its own try/except so a single failure
    (e.g. AccessDenied on process scan) never crashes the whole loop.
    """
    # Prime the CPU percent cache (first call always returns 0)
    get_cpu_usage()
    get_per_core_usage()
    await asyncio.sleep(1)

    while True:
        # --- CPU (isolated) ---
        try:
            cpu = get_cpu_usage()
            per_core = get_per_core_usage()
        except Exception:
            logger.exception("CPU collection failed")
            cpu, per_core = 0.0, []

        # --- Memory (isolated) ---
        try:
            mem = get_memory_usage()
        except Exception:
            logger.exception("Memory collection failed")
            mem = {
                "total": 0,
                "used": 0,
                "available": 0,
                "percent": 0.0,
                "pressure": 0.0,
                "swap_used": 0,
                "swap_total": 0,
                "swap_percent": 0.0,
            }

        # --- Disk / Network / Load (isolated) ---
        try:
            disk = get_disk_io_rates()
        except Exception:
            logger.exception("Disk collection failed")
            disk = {"read_mb_s": 0.0, "write_mb_s": 0.0}

        try:
            net = get_network_io_rates()
        except Exception:
            logger.exception("Network collection failed")
            net = {"up_mb_s": 0.0, "down_mb_s": 0.0}

        try:
            load = get_load_average()
        except Exception:
            logger.exception("Load average collection failed")
            load = {"one": 0.0, "five": 0.0, "fifteen": 0.0}

        state.last_disk = disk
        state.last_network = net
        state.last_load = load

        # --- Processes (isolated + already throttled in collector) ---
        try:
            procs = get_process_list()
        except Exception:
            logger.exception("Process collection failed")
            procs = []

        state.history.add(cpu, mem["percent"])
        state.last_cpu = cpu
        state.last_mem = mem["percent"]
        state.last_proc_count = len(procs)

        alerts = _build_alerts(
            cpu=cpu,
            mem_pct=mem["percent"],
            mem_pressure=mem.get("pressure", mem["percent"]),
            disk=disk,
            net=net,
            load=load,
        )

        payload = {
            "cpu": {
                "overall": cpu,
                "per_core": per_core,
            },
            "memory": {
                "percent": mem["percent"],
                "used": format_bytes(mem["used"]),
                "total": format_bytes(mem["total"]),
                "pressure": mem.get("pressure", mem["percent"]),
                "swap_used": format_bytes(mem["swap_used"]),
                "swap_total": format_bytes(mem["swap_total"]),
                "swap_percent": mem["swap_percent"],
            },
            "disk": disk,
            "network": net,
            "load": load,
            "history": {
                "cpu": state.history.get_cpu_history(),
                "memory": state.history.get_memory_history(),
            },
            "processes": sorted(procs, key=lambda p: p["cpu"], reverse=True)[:200],
            "system_info": state.system_info,  # from slow loop
            "status": _compute_status(cpu, mem["percent"]),
            "alerts": alerts,
        }

        await _broadcast(payload)
        await asyncio.sleep(state.min_interval)


async def _slow_metric_loop():
    """Refresh expensive / semi-static system info every SLOW_METRIC_INTERVAL."""
    while True:
        try:
            state.system_info = get_system_info()
        except Exception:
            logger.exception("System info collection failed")
        state.last_slow_update = time.time()
        await asyncio.sleep(SLOW_METRIC_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
