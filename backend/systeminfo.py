"""
systeminfo.py — Static + dynamic machine information.

Static data (OS, CPU model, cores, RAM, disk capacity) is collected
once at import time and cached.  Only dynamic fields (uptime, running
process count) are recomputed on each call.
"""

import platform
import time
from functools import lru_cache

import psutil

from .processor import format_bytes, format_uptime


# ---------------------------------------------------------------------------
# Static info — computed once, never changes during runtime
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_cpu_model() -> str:
    """Best-effort CPU model string.  Falls back to arch if unavailable."""
    try:
        if platform.system() == "Darwin":
            import subprocess

            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        elif platform.system() == "Windows":
            return platform.processor() or "Unknown CPU"
        else:
            # Linux — read model name via platform or /proc fallback
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("model name"):
                            return line.split(":")[1].strip()
            except OSError:
                pass
    except Exception:
        pass
    return platform.processor() or platform.machine() or "Unknown CPU"


@lru_cache(maxsize=1)
def _collect_static_info() -> dict:
    """Gather hardware/OS facts that never change at runtime."""
    disk = psutil.disk_usage("/")
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu_model": _get_cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False) or 0,
        "logical_threads": psutil.cpu_count(logical=True) or 0,
        "total_ram": format_bytes(psutil.virtual_memory().total),
        "total_disk": format_bytes(disk.total),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_system_info() -> dict:
    """Return static info merged with live dynamic fields."""
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time

    info = dict(_collect_static_info())  # shallow copy of cached dict
    info["uptime"] = format_uptime(uptime_seconds)
    info["running_processes"] = len(psutil.pids())
    return info
