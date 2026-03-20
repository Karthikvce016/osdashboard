"""
collector.py — System metric collection using psutil.

All functions return raw data (no formatting). Cross-platform;
uses psutil exclusively for portability.

Process scanning is throttled to reduce overhead on systems with
hundreds of processes.
"""

import time
from typing import Dict, List, Tuple, Optional

import psutil

# ---------------------------------------------------------------------------
# Process scan throttle — avoids expensive iteration every cycle
# ---------------------------------------------------------------------------

_process_cache: List[Dict] = []
_last_process_scan: float = 0.0
PROCESS_SCAN_INTERVAL: float = 3.0  # seconds

# ---------------------------------------------------------------------------
# Protected / risky process names (case-insensitive match)
# Killing these can destabilise the system or crash the desktop session.
# ---------------------------------------------------------------------------

RISKY_PROCESSES: set[str] = {
    # macOS
    "kernel_task", "launchd", "windowserver", "dock", "finder",
    "systemuiserver", "loginwindow", "coreaudiod", "coreservicesd",
    "cfprefsd", "distnoted", "logd", "opendirectoryd", "fseventsd",
    "mds", "mds_stores", "spotlight", "airplayxpchelper",
    # Linux
    "systemd", "init", "kthreadd", "rcu_sched", "ksoftirqd",
    "kworker", "dbus-daemon", "networkmanager", "gdm", "sddm",
    "xorg", "xwayland", "pulseaudio", "pipewire",
    # Windows
    "system", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe",
    "winlogon.exe", "taskhostw.exe",
}


def is_risky_process(name: str) -> bool:
    """Check whether a process name belongs to the protected set."""
    return name.lower() in RISKY_PROCESSES


# ---------------------------------------------------------------------------
# CPU metrics
# ---------------------------------------------------------------------------


def get_cpu_usage() -> float:
    """Return overall CPU usage percentage (non-blocking, uses cached value)."""
    return psutil.cpu_percent(interval=None)


def get_per_core_usage() -> List[float]:
    """Return per-core CPU usage percentages."""
    return psutil.cpu_percent(percpu=True)


# ---------------------------------------------------------------------------
# Memory metrics
# ---------------------------------------------------------------------------


def get_memory_usage() -> dict:
    """Return memory statistics as a dictionary with pressure estimate."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    ram_pressure = mem.percent
    swap_pressure = swap.percent * 0.7
    pressure = max(ram_pressure, swap_pressure)

    return {
        "total": mem.total,
        "used": mem.used,
        "available": mem.available,
        "percent": mem.percent,
        "pressure": round(pressure, 1),
        "swap_used": swap.used,
        "swap_total": swap.total,
        "swap_percent": swap.percent,
    }


# ---------------------------------------------------------------------------
# Disk and network I/O (rate-based)
# ---------------------------------------------------------------------------

_last_disk: Optional[Tuple[int, int, float]] = None  # read_bytes, write_bytes, ts
_last_net: Optional[Tuple[int, int, float]] = None  # bytes_sent, bytes_recv, ts


def get_disk_io_rates() -> Dict[str, float]:
    """Return disk read/write throughput in MB/s."""
    global _last_disk
    now = time.time()
    counters = psutil.disk_io_counters()
    if counters is None:
        return {"read_mb_s": 0.0, "write_mb_s": 0.0}

    read_bytes = counters.read_bytes
    write_bytes = counters.write_bytes

    if _last_disk is None:
        _last_disk = (read_bytes, write_bytes, now)
        return {"read_mb_s": 0.0, "write_mb_s": 0.0}

    last_read, last_write, last_ts = _last_disk
    dt = max(now - last_ts, 1e-3)
    read_mb_s = max((read_bytes - last_read) / dt / (1024 * 1024), 0.0)
    write_mb_s = max((write_bytes - last_write) / dt / (1024 * 1024), 0.0)

    _last_disk = (read_bytes, write_bytes, now)
    return {
        "read_mb_s": round(read_mb_s, 2),
        "write_mb_s": round(write_mb_s, 2),
    }


def get_network_io_rates() -> Dict[str, float]:
    """Return network upload/download throughput in MB/s."""
    global _last_net
    now = time.time()
    counters = psutil.net_io_counters()
    if counters is None:
        return {"up_mb_s": 0.0, "down_mb_s": 0.0}

    sent = counters.bytes_sent
    recv = counters.bytes_recv

    if _last_net is None:
        _last_net = (sent, recv, now)
        return {"up_mb_s": 0.0, "down_mb_s": 0.0}

    last_sent, last_recv, last_ts = _last_net
    dt = max(now - last_ts, 1e-3)
    up_mb_s = max((sent - last_sent) / dt / (1024 * 1024), 0.0)
    down_mb_s = max((recv - last_recv) / dt / (1024 * 1024), 0.0)

    _last_net = (sent, recv, now)
    return {
        "up_mb_s": round(up_mb_s, 2),
        "down_mb_s": round(down_mb_s, 2),
    }


# ---------------------------------------------------------------------------
# Load average
# ---------------------------------------------------------------------------


def get_load_average() -> Dict[str, float]:
    """Return 1m, 5m, 15m CPU load averages if supported by the OS."""
    try:
        import os

        one, five, fifteen = os.getloadavg()
        return {
            "one": round(one, 2),
            "five": round(five, 2),
            "fifteen": round(fifteen, 2),
        }
    except (OSError, AttributeError):
        return {"one": 0.0, "five": 0.0, "fifteen": 0.0}


# ---------------------------------------------------------------------------
# Process list (throttled)
# ---------------------------------------------------------------------------


def classify_process(name: str, username: Optional[str], nice: Optional[int]) -> str:
    """Classify process as 'system', 'user', or 'background'."""
    if is_risky_process(name) or (username and username.lower() in {"root", "system", "localservice", "networkservice"}):
        return "system"
    if nice is not None and nice > 0:
        return "background"
    return "user"


def get_process_list() -> List[Dict]:
    """
    Return a list of running processes with:
    - pid
    - name
    - cpu (percent)
    - memory (percent)
    - risky (bool)
    - type: "system" | "user" | "background"

    Results are cached for PROCESS_SCAN_INTERVAL seconds to avoid
    expensive iteration on every metric cycle.  Processes that vanish
    mid-iteration are silently skipped.
    """
    global _process_cache, _last_process_scan

    now = time.time()
    if now - _last_process_scan < PROCESS_SCAN_INTERVAL and _process_cache:
        return _process_cache

    procs: List[Dict] = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "username", "nice"]):
        try:
            info = proc.info
            pname = info["name"] or "Unknown"
            risky = is_risky_process(pname)
            ptype = classify_process(pname, info.get("username"), info.get("nice"))
            procs.append(
                {
                    "pid": info["pid"],
                    "name": pname,
                    "cpu": round(info.get("cpu_percent") or 0.0, 1),
                    "memory": round(info.get("memory_percent") or 0.0, 1),
                    "risky": risky,
                    "type": ptype,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    _process_cache = procs
    _last_process_scan = now
    return procs
