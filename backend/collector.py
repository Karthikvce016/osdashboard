"""
collector.py — System metric collection using psutil.

All functions return raw data (no formatting). Cross-platform;
uses psutil exclusively for portability.

Process scanning is throttled to reduce overhead on systems with
hundreds of processes.
"""

import os
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
# CPU stats — context switches, interrupts, user/kernel time (Fix 3)
# ---------------------------------------------------------------------------


def get_cpu_stats() -> dict:
    """
    Return context switches, interrupts, and user/kernel/idle time split.
    cpu_stats()  → context switches + hardware/software interrupts
    cpu_times()  → time breakdown (user, system/kernel, idle, iowait on Linux)
    """
    stats = psutil.cpu_stats()
    times = psutil.cpu_times()

    result = {
        "ctx_switches":    stats.ctx_switches,      # total since boot
        "interrupts":      stats.interrupts,         # hardware interrupts since boot
        "soft_interrupts": getattr(stats, "soft_interrupts", 0),
        "user_time":   round(times.user, 2),
        "system_time": round(times.system, 2),
        "idle_time":   round(times.idle, 2),
        "iowait":      round(getattr(times, "iowait", 0.0), 2),  # Linux only
    }
    return result


_last_cpu_stats: Optional[dict] = None
_last_cpu_stats_time: float = 0.0


def get_cpu_stat_rates() -> dict:
    """Return context switches/sec and interrupts/sec as rates."""
    global _last_cpu_stats, _last_cpu_stats_time
    now = time.time()
    raw = get_cpu_stats()

    if _last_cpu_stats is None:
        _last_cpu_stats = raw
        _last_cpu_stats_time = now
        return {"ctx_switches_per_sec": 0, "interrupts_per_sec": 0, **raw}

    dt = max(now - _last_cpu_stats_time, 1e-3)
    ctx_rate = (raw["ctx_switches"] - _last_cpu_stats["ctx_switches"]) / dt
    irq_rate = (raw["interrupts"]   - _last_cpu_stats["interrupts"])   / dt

    _last_cpu_stats = raw
    _last_cpu_stats_time = now

    return {
        **raw,
        "ctx_switches_per_sec": round(max(ctx_rate, 0), 1),
        "interrupts_per_sec":   round(max(irq_rate, 0), 1),
    }


# ---------------------------------------------------------------------------
# Memory metrics (Fix 7 — buffers/cached/page fault rate)
# ---------------------------------------------------------------------------


_last_page_faults: Optional[Tuple[int, int, float]] = None  # minor, major, ts


def get_memory_usage() -> dict:
    """Return memory statistics with pressure estimate, buffers/cached, swap rates."""
    global _last_page_faults
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Use swap in/out as the page fault rate proxy — works cross-platform
    now = time.time()
    swap_in  = getattr(swap, "sin",  0)   # pages swapped in
    swap_out = getattr(swap, "sout", 0)   # pages swapped out
    swap_in_rate = swap_out_rate = 0.0

    if _last_page_faults is not None:
        last_in, last_out, last_ts = _last_page_faults
        dt = max(now - last_ts, 1e-3)
        swap_in_rate  = max((swap_in  - last_in)  / dt, 0.0)
        swap_out_rate = max((swap_out - last_out) / dt, 0.0)

    _last_page_faults = (swap_in, swap_out, now)

    ram_pressure  = mem.percent
    swap_pressure = swap.percent * 0.7
    pressure      = max(ram_pressure, swap_pressure)

    return {
        "total":          mem.total,
        "used":           mem.used,
        "available":      mem.available,
        "percent":        mem.percent,
        "buffers":        getattr(mem, "buffers", 0),   # Linux only
        "cached":         getattr(mem, "cached",  0),   # Linux only
        "shared":         getattr(mem, "shared",  0),
        "pressure":       round(pressure, 1),
        "swap_used":      swap.used,
        "swap_total":     swap.total,
        "swap_percent":   swap.percent,
        "swap_in_rate":   round(swap_in_rate, 2),    # pages/sec in
        "swap_out_rate":  round(swap_out_rate, 2),   # pages/sec out
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
# Disk partitions with inode stats (Fix 6)
# ---------------------------------------------------------------------------


def get_disk_partitions() -> List[Dict]:
    """
    Return per-partition disk usage including inode stats.
    Skips pseudo-filesystems (proc, sysfs, devtmpfs, etc.)

    Inodes: every file/directory consumes exactly one inode.
    When inodes run out, you can't create new files even if space remains.
    """
    SKIP_FSTYPES = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs",
                    "cgroup", "cgroup2", "pstore", "debugfs",
                    "securityfs", "fusectl", "hugetlbfs", "mqueue"}
    results = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in SKIP_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)

            # Inode stats (Unix only — os.statvfs)
            inodes_used  = 0
            inodes_total = 0
            inodes_pct   = 0.0
            try:
                sv = os.statvfs(part.mountpoint)
                inodes_total = sv.f_files
                inodes_free  = sv.f_ffree
                inodes_used  = inodes_total - inodes_free
                inodes_pct   = round(inodes_used / inodes_total * 100, 1) if inodes_total > 0 else 0.0
            except (AttributeError, ZeroDivisionError, OSError):
                pass  # Windows doesn't have statvfs

            results.append({
                "mountpoint":   part.mountpoint,
                "fstype":       part.fstype,
                "total":        usage.total,
                "used":         usage.used,
                "free":         usage.free,
                "percent":      usage.percent,
                "inodes_used":  inodes_used,
                "inodes_total": inodes_total,
                "inodes_pct":   inodes_pct,
            })
        except (PermissionError, OSError):
            continue
    return results


# ---------------------------------------------------------------------------
# Load average
# ---------------------------------------------------------------------------


def get_load_average() -> Dict[str, float]:
    """Return 1m, 5m, 15m CPU load averages if supported by the OS."""
    try:
        one, five, fifteen = os.getloadavg()
        return {
            "one": round(one, 2),
            "five": round(five, 2),
            "fifteen": round(fifteen, 2),
        }
    except (OSError, AttributeError):
        return {"one": 0.0, "five": 0.0, "fifteen": 0.0}


# ---------------------------------------------------------------------------
# Synchronization & Deadlock stats (Fix 5)
# ---------------------------------------------------------------------------


def get_sync_stats() -> dict:
    """
    Aggregate synchronization and resource contention metrics.

    D-state (disk-sleep / STATUS_DISK_SLEEP) = process is blocked waiting
    for I/O to complete and cannot be interrupted — the closest observable
    proxy for deadlock / severe resource contention in a running system.

    Voluntary ctx switches   = process willingly yielded CPU (e.g. waiting for I/O).
    Involuntary ctx switches = scheduler preempted the process (e.g. time slice expired).
    A high involuntary ratio indicates heavy CPU contention between threads.
    """
    thread_states = {"running": 0, "sleeping": 0, "disk-sleep": 0,
                     "stopped": 0, "zombie": 0, "other": 0}
    total_threads  = 0
    total_fds      = 0
    d_state_procs  = []   # PIDs stuck in uninterruptible sleep
    vol_ctx_total  = 0
    invol_ctx_total = 0

    for proc in psutil.process_iter(["pid", "name", "status", "num_threads"]):
        try:
            info  = proc.info
            status = info.get("status", "other")
            nthreads = info.get("num_threads", 0) or 0
            total_threads += nthreads

            # Count by state
            if status in thread_states:
                thread_states[status] += 1
            else:
                thread_states["other"] += 1

            # D-state = blocked, uninterruptible
            if status == psutil.STATUS_DISK_SLEEP:
                d_state_procs.append({
                    "pid":  info["pid"],
                    "name": info["name"] or "Unknown",
                })

            # File descriptors (Unix)
            try:
                total_fds += proc.num_fds()
            except (AttributeError, psutil.AccessDenied):
                pass

            # Context switches
            try:
                ctx = proc.num_ctx_switches()
                vol_ctx_total   += ctx.voluntary
                invol_ctx_total += ctx.involuntary
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # System-wide FD limit (Unix only)
    try:
        import resource
        fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    except Exception:
        fd_limit = -1

    total_ctx = vol_ctx_total + invol_ctx_total
    invol_ratio = round(invol_ctx_total / total_ctx * 100, 1) if total_ctx > 0 else 0.0

    return {
        "thread_states":    thread_states,
        "total_threads":    total_threads,
        "total_fds":        total_fds,
        "fd_limit":         fd_limit,
        "d_state_procs":    d_state_procs[:10],   # cap at 10 for payload size
        "d_state_count":    len(d_state_procs),
        "vol_ctx_total":    vol_ctx_total,
        "invol_ctx_total":  invol_ctx_total,
        "invol_ctx_ratio":  invol_ratio,           # % of ctx switches that were involuntary
    }


# ---------------------------------------------------------------------------
# Process list (throttled) — Fix 4: extended with RSS, VMS, state, threads
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
    - pid, name, cpu, memory, rss, status, threads, fds
    - vol_ctx, invol_ctx (per-process context switches)
    - risky (bool), type: "system" | "user" | "background"

    Results are cached for PROCESS_SCAN_INTERVAL seconds to avoid
    expensive iteration on every metric cycle.
    """
    global _process_cache, _last_process_scan

    now = time.time()
    if now - _last_process_scan < PROCESS_SCAN_INTERVAL and _process_cache:
        return _process_cache

    procs: List[Dict] = []
    attrs = ["pid", "name", "cpu_percent", "memory_percent",
             "memory_info", "username", "nice", "status", "num_threads"]

    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            pname = info["name"] or "Unknown"
            risky = is_risky_process(pname)
            ptype = classify_process(pname, info.get("username"), info.get("nice"))

            mem_info = info.get("memory_info")
            rss = mem_info.rss if mem_info else 0

            # Voluntary vs non-voluntary context switches (per-process)
            try:
                ctx = proc.num_ctx_switches()
                vol_ctx   = ctx.voluntary
                invol_ctx = ctx.involuntary
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                vol_ctx = invol_ctx = 0

            # Open file descriptors (Unix only)
            try:
                fds = proc.num_fds()
            except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess):
                fds = -1  # -1 = unavailable (Windows)

            procs.append({
                "pid":      info["pid"],
                "name":     pname,
                "cpu":      round(info.get("cpu_percent") or 0.0, 1),
                "memory":   round(info.get("memory_percent") or 0.0, 1),
                "rss":      rss,          # bytes — format in frontend
                "status":   info.get("status", "?"),   # running/sleeping/zombie/disk-sleep
                "threads":  info.get("num_threads", 0),
                "fds":      fds,
                "vol_ctx":  vol_ctx,
                "invol_ctx": invol_ctx,
                "risky":    risky,
                "type":     ptype,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    _process_cache = procs
    _last_process_scan = now
    return procs
